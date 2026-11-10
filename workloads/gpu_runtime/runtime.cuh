#pragma once
#include <cooperative_groups.h>
#include <cuda_runtime.h>
#include <cmath>

namespace damer {
namespace cg = cooperative_groups;
enum class Op : int { Wave, Scale, Add };
enum Error : int { Ok, NoReadyTask, Capacity, InvalidLifetime, NonFinite };
enum Phase : int { Empty, Building, Published, Retired };
struct Task {
  Op op;
  int input0, input1, output;
  float a, b;
};
struct Object {
  int slot, remaining, pins, roots, phase;
};
struct State {
  int current, error, finished, occupied, peak_slots;
  unsigned long long publications, retirements, completed_tasks, completed_runs;
};
struct View {
  const Task* tasks;
  const int* uses;
  const int* roots;
  const float* seed;
  Object* objects;
  int* done;
  int* owners;
  float* pool;
  State* state;
  int task_count, object_count, slots, elements, width;
};

// Every grid barrier is unconditional. The host caps the cooperative grid to
// occupancy, so all blocks can reside concurrently. Only grid thread 0 mutates
// scheduling/lifetime metadata; barriers publish it to the other blocks.
__device__ inline void reset(View v, cg::grid_group grid) {
  for (int i = grid.thread_rank(); i < v.object_count; i += grid.size())
    v.objects[i] = {-1, v.uses[i], 0, v.roots[i], Empty};
  for (int i = grid.thread_rank(); i < v.task_count; i += grid.size()) v.done[i] = 0;
  for (int i = grid.thread_rank(); i < v.slots; i += grid.size()) v.owners[i] = -1;
  for (int i = grid.thread_rank(); i < v.elements; i += grid.size()) v.pool[i] = v.seed[i];
  grid.sync();
  if (grid.thread_rank() == 0) {
    v.objects[0].slot = 0;
    v.objects[0].phase = Published;
    v.owners[0] = 0;
    v.state->finished = 0;
    v.state->occupied = 1;
    v.state->peak_slots = max(v.state->peak_slots, 1);
    v.state->current = -1;
  }
  grid.sync();
}

__device__ inline bool ready(const View& v, int id) {
  return id < 0 || v.objects[id].phase == Published;
}
__device__ inline void admit(View v) {
  State& s = *v.state;
  s.current = -1;
  if (s.error) return;
  for (int i = 0; i < v.task_count; ++i) {
    const Task& t = v.tasks[i];
    if (!v.done[i] && ready(v, t.input0) && ready(v, t.input1)) {
      if (v.objects[t.output].phase != Empty) { s.error = InvalidLifetime; return; }
      int slot = -1;
      for (int j = 0; j < v.slots; ++j) if (v.owners[j] == -1) { slot = j; break; }
      if (slot < 0) { s.error = Capacity; return; }
      int ids[2] = {t.input0, t.input1};
      for (int j = 0; j < 2; ++j) {
        int id = ids[j];
        if (id >= 0 && v.objects[id].remaining <= 0) { s.error = InvalidLifetime; return; }
      }
      for (int j = 0; j < 2; ++j) if (ids[j] >= 0) ++v.objects[ids[j]].pins;
      v.owners[slot] = t.output;
      v.objects[t.output].slot = slot;
      v.objects[t.output].phase = Building;
      ++s.occupied;
      s.peak_slots = max(s.peak_slots, s.occupied);
      s.current = i;
      return;
    }
  }
  s.error = NoReadyTask;
}

__device__ inline void retire(View v, int id) {
  Object& o = v.objects[id];
  if (o.phase == Published && o.remaining == 0 && o.pins == 0 && o.roots == 0) {
    v.owners[o.slot] = -1;
    o.slot = -1;
    o.phase = Retired;
    --v.state->occupied;
    ++v.state->retirements;
  }
}

__device__ inline void execute(View v, cg::grid_group grid) {
  if (grid.thread_rank() == 0) admit(v);
  grid.sync();
  if (v.state->current >= 0) {
    const Task t = v.tasks[v.state->current];
    const float* x = v.pool + size_t(v.objects[t.input0].slot) * v.elements;
    const float* y = t.input1 < 0 ? nullptr : v.pool + size_t(v.objects[t.input1].slot) * v.elements;
    float* out = v.pool + size_t(v.objects[t.output].slot) * v.elements;
    // Wave ports kernels.py's periodic semi-implicit Euler update. The two
    // channels are one immutable version; both are published together.
    int count = t.op == Op::Wave ? v.elements / 2 : v.elements;
    for (int i = grid.thread_rank(); i < count; i += grid.size()) {
      float z;
      if (t.op == Op::Wave) {
        int n = v.width, row = i / n, col = i % n;
        float lap = x[((row + n - 1) % n)*n + col] + x[((row + 1) % n)*n + col]
                  + x[row*n + (col + n - 1)%n] + x[row*n + (col + 1)%n] - 4.f*x[i];
        float velocity = x[count+i] + .2f*((.5f+t.a)*(.5f+t.a)*lap - (.01f+.09f*t.b)*x[count+i]);
        z = x[i] + .2f*velocity;
        out[count+i] = velocity;
        if (!isfinite(velocity)) atomicExch(&v.state->error, int(NonFinite));
      } else if (t.op == Op::Scale) z = t.a * x[i];
      else z = x[i] + y[i];
      out[i] = z;
      if (!isfinite(z)) atomicExch(&v.state->error, int(NonFinite));
    }
  }
  grid.sync(); // No outstanding payload access may survive slot reclamation.
  if (grid.thread_rank() == 0 && !v.state->error) {
    const Task& t = v.tasks[v.state->current];
    v.objects[t.output].phase = Published;
    ++v.state->publications;
    int ids[2] = {t.input0, t.input1};
    for (int j = 0; j < 2; ++j) if (ids[j] >= 0) {
      Object& o = v.objects[ids[j]];
      --o.pins;
      --o.remaining;
      retire(v, ids[j]);
    }
    retire(v, t.output);
    v.done[v.state->current] = 1;
    ++v.state->finished;
    ++v.state->completed_tasks;
  }
  grid.sync();
}

__global__ void persistent(View v, int repeats) {
  auto grid = cg::this_grid();
  for (int r = 0; r < repeats; ++r) {
    reset(v, grid);
    for (int i = 0; i < v.task_count; ++i) {
      execute(v, grid);
      if (v.state->error) return; // Uniform after grid barrier.
    }
    if (grid.thread_rank() == 0) ++v.state->completed_runs;
    grid.sync();
    // The benchmark releases prior-run roots here only after the entire grid
    // has drained. Each repetition is an independent run of the accepted DAG.
  }
}

__global__ void host_reset(View v) { reset(v, cg::this_grid()); }
__global__ void host_step(View v) { execute(v, cg::this_grid()); }
__global__ void host_finish(View v) {
  if (blockIdx.x == 0 && threadIdx.x == 0 && !v.state->error) ++v.state->completed_runs;
}
} // namespace damer
