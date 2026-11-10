#include "runtime.cuh"
#include <algorithm>
#include <chrono>
#include <ctime>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <limits>
#include <stdexcept>
#include <string>
#include <vector>

#define CUDA(call) do { cudaError_t e = (call); if (e != cudaSuccess) \
  throw std::runtime_error(std::string(#call) + ": " + cudaGetErrorString(e)); } while (0)

using namespace damer;
template<class T> struct Device {
  T* p = nullptr;
  explicit Device(size_t n) { CUDA(cudaMalloc(&p, n*sizeof(T))); }
  ~Device() { cudaFree(p); }
  Device(const Device&) = delete;
  void upload(const std::vector<T>& v) { CUDA(cudaMemcpy(p, v.data(), v.size()*sizeof(T), cudaMemcpyHostToDevice)); }
};
struct Plan {
  std::vector<Task> tasks;
  std::vector<int> uses, roots;
  std::vector<float> seed;
  int width, output;
  void validate() {
    const int objects = int(roots.size());
    if (objects < 2 || output <= 0 || output >= objects || !roots[output])
      throw std::runtime_error("invalid rooted output");
    std::vector<int> producer(objects, -1), available(objects, 0), done(tasks.size(), 0);
    uses.assign(objects, 0);
    available[0] = 1;
    for (size_t i = 0; i < tasks.size(); ++i) {
      auto t = tasks[i];
      if (t.output <= 0 || t.output >= objects || producer[t.output] != -1)
        throw std::runtime_error("invalid or duplicate producer");
      if (t.input0 < 0 || t.input0 >= objects || t.input1 < -1 || t.input1 >= objects ||
          t.input0 == t.output || t.input1 == t.output || t.input0 == t.input1)
        throw std::runtime_error("invalid/aliased object handle");
      if ((t.op == Op::Add) != (t.input1 >= 0)) throw std::runtime_error("invalid arity");
      if (!std::isfinite(t.a) || !std::isfinite(t.b)) throw std::runtime_error("nonfinite parameter");
      producer[t.output] = int(i);
      ++uses[t.input0];
      if (t.input1 >= 0) ++uses[t.input1];
    }
    for (int i = 1; i < objects; ++i)
      if (producer[i] == -1) throw std::runtime_error("missing producer");
    for (size_t k = 0; k < tasks.size(); ++k) {
      bool found = false;
      for (size_t i = 0; i < tasks.size(); ++i) {
        auto t = tasks[i];
        if (!done[i] && available[t.input0] && (t.input1 < 0 || available[t.input1])) {
          done[i] = available[t.output] = 1;
          found = true;
          break;
        }
      }
      if (!found) throw std::runtime_error("cycle or unreachable input");
    }
  }
};

Plan make_plan(const std::string& kind, int n, int steps) {
  Plan p;
  p.width = n;
  p.seed.resize(size_t(2)*n*n);
  for (int r = 0; r < n; ++r) for (int c = 0; c < n; ++c) {
    float x = (float(c)-float(n)/2)/n, y = (float(r)-float(n)/2)/n;
    p.seed[r*n+c] = std::exp(-100.f*(x*x+y*y));
  }
  if (kind == "wave") {
    for (int i = 0; i < steps; ++i) p.tasks.push_back({Op::Wave, i, -1, i+1, .3f, .7f});
    p.output = steps;
  } else if (kind == "diamond") {
    // Deliberately non-topological descriptor order exercises GPU readiness.
    p.tasks = {{Op::Add, 1, 2, 3, 0, 0}, {Op::Scale, 0, -1, 1, 2, 0},
               {Op::Scale, 0, -1, 2, 3, 0}};
    p.output = 3;
  } else throw std::runtime_error("workload must be wave or diamond");
  p.roots.assign(p.tasks.size()+1, 0);
  p.roots[p.output] = 1;
  p.validate();
  return p;
}

std::vector<float> reference(const Plan& p, const std::string& kind) {
  auto x = p.seed;
  if (kind == "diamond") { for (float& f : x) f *= 5.f; return x; }
  std::vector<float> out(x.size());
  int n = p.width, count = n*n;
  for (const Task& t : p.tasks) {
    for (int r = 0; r < n; ++r) for (int c = 0; c < n; ++c) {
      int i = r*n+c;
      float lap = x[((r+n-1)%n)*n+c] + x[((r+1)%n)*n+c]
                + x[r*n+(c+n-1)%n] + x[r*n+(c+1)%n] - 4.f*x[i];
      float v = x[count+i] + .2f*((.5f+t.a)*(.5f+t.a)*lap-(.01f+.09f*t.b)*x[count+i]);
      out[count+i] = v;
      out[i] = x[i]+.2f*v;
    }
    x.swap(out);
  }
  return x;
}

double process_seconds() {
  timespec ts{};
  if (clock_gettime(CLOCK_PROCESS_CPUTIME_ID, &ts)) throw std::runtime_error("process clock failed");
  return double(ts.tv_sec) + double(ts.tv_nsec)*1.e-9;
}

int main(int argc, char** argv) try {
  std::string mode = "persistent", kind = "wave", output, fault;
  int n = 64, steps = 64, repeats = 10, slots = 3;
  for (int i = 1; i < argc; ++i) {
    std::string key = argv[i];
    if (key == "--help") {
      std::cout << "--mode persistent|host|graph --workload wave|diamond --n 64 --steps 64 --repeats 10 --slots 3 --output FILE --fault cycle|capacity|uses|nan\n";
      return 0;
    }
    if (++i == argc) throw std::runtime_error("missing option value");
    std::string value = argv[i];
    if (key == "--mode") mode = value;
    else if (key == "--workload") kind = value;
    else if (key == "--output") output = value;
    else if (key == "--fault") fault = value;
    else {
      size_t consumed;
      int v = std::stoi(value, &consumed);
      if (consumed != value.size()) throw std::runtime_error("invalid integer");
      if (key == "--n") n = v;
      else if (key == "--steps") steps = v;
      else if (key == "--repeats") repeats = v;
      else if (key == "--slots") slots = v;
      else throw std::runtime_error("unknown option: " + key);
    }
  }
  if (mode != "persistent" && mode != "host" && mode != "graph") throw std::runtime_error("invalid mode");
  if (n < 2 || n > 1024 || steps < 1 || steps > 4096 || repeats < 1 || repeats > 10000 || slots < 1 || slots > 16)
    throw std::runtime_error("out-of-range workload size");
  if (!fault.empty() && fault != "cycle" && fault != "capacity" && fault != "uses" && fault != "nan")
    throw std::runtime_error("unknown fault");
  Plan p = make_plan(kind, n, steps);
  if (fault == "cycle") { p.tasks[0].input0 = p.output; p.validate(); }
  if (fault == "capacity") slots = 1;
  if (fault == "uses") p.uses[0] = 0; // Corruption after trusted admission.
  if (fault == "nan") p.seed[0] = std::numeric_limits<float>::quiet_NaN();

  CUDA(cudaSetDeviceFlags(cudaDeviceScheduleBlockingSync));
  cudaDeviceProp props{};
  CUDA(cudaGetDeviceProperties(&props, 0));
  if (!props.cooperativeLaunch) throw std::runtime_error("cooperative launch required; no CPU fallback");
  int runtime_version, driver_version;
  CUDA(cudaRuntimeGetVersion(&runtime_version)); CUDA(cudaDriverGetVersion(&driver_version));
  Device<Task> tasks(p.tasks.size()); tasks.upload(p.tasks);
  Device<int> uses(p.uses.size()), roots(p.roots.size()), done(p.tasks.size()), owners(slots);
  uses.upload(p.uses); roots.upload(p.roots);
  Device<float> seed(p.seed.size()), pool(size_t(slots)*p.seed.size()); seed.upload(p.seed);
  Device<Object> objects(p.roots.size()); Device<State> state(1);
  View v{tasks.p, uses.p, roots.p, seed.p, objects.p, done.p, owners.p, pool.p, state.p,
         int(p.tasks.size()), int(p.roots.size()), slots, int(p.seed.size()), n};
  int active, reset_active, step_active;
  constexpr int threads = 256;
  CUDA(cudaOccupancyMaxActiveBlocksPerMultiprocessor(&active, persistent, threads, 0));
  CUDA(cudaOccupancyMaxActiveBlocksPerMultiprocessor(&reset_active, host_reset, threads, 0));
  CUDA(cudaOccupancyMaxActiveBlocksPerMultiprocessor(&step_active, host_step, threads, 0));
  int blocks = std::min({active, reset_active, step_active}) * props.multiProcessorCount;
  blocks = std::min(blocks, (v.elements + threads-1)/threads);
  if (blocks < 1) throw std::runtime_error("no cooperative occupancy");
  cudaStream_t stream; CUDA(cudaStreamCreateWithFlags(&stream, cudaStreamNonBlocking));
  cudaEvent_t start, stop;
  CUDA(cudaEventCreate(&start)); CUDA(cudaEventCreateWithFlags(&stop, cudaEventBlockingSync));
  auto launch_persistent = [&](int count) {
    void* args[] = {&v, &count};
    CUDA(cudaLaunchCooperativeKernel((const void*)persistent, blocks, threads, args, 0, stream));
  };
  auto launch_host = [&]() {
    void* args[] = {&v};
    CUDA(cudaLaunchCooperativeKernel((const void*)host_reset, blocks, threads, args, 0, stream));
    for (size_t i = 0; i < p.tasks.size(); ++i)
      CUDA(cudaLaunchCooperativeKernel((const void*)host_step, blocks, threads, args, 0, stream));
    host_finish<<<1,1,0,stream>>>(v);
    CUDA(cudaGetLastError());
  };
  // Warm up allocations/JIT and synchronize before every measured mode.
  CUDA(cudaMemsetAsync(state.p, 0, sizeof(State), stream));
  launch_persistent(1);
  CUDA(cudaStreamSynchronize(stream));
  cudaGraph_t graph = nullptr; cudaGraphExec_t executable = nullptr;
  if (mode == "graph") {
    CUDA(cudaStreamBeginCapture(stream, cudaStreamCaptureModeThreadLocal));
    for (int i = 0; i < repeats; ++i) launch_host();
    CUDA(cudaStreamEndCapture(stream, &graph));
    CUDA(cudaGraphInstantiate(&executable, graph, 0));
    CUDA(cudaGraphUpload(executable, stream));
    CUDA(cudaStreamSynchronize(stream));
  }
  CUDA(cudaMemsetAsync(state.p, 0, sizeof(State), stream));
  CUDA(cudaStreamSynchronize(stream));
  auto wall_start = std::chrono::steady_clock::now();
  double cpu_start = process_seconds();
  CUDA(cudaEventRecord(start, stream));
  if (mode == "persistent") launch_persistent(repeats);
  else if (mode == "graph") CUDA(cudaGraphLaunch(executable, stream));
  else for (int i = 0; i < repeats; ++i) launch_host();
  CUDA(cudaEventRecord(stop, stream));
  CUDA(cudaEventSynchronize(stop)); // Blocking host wait, not a CPU progress loop.
  double cpu_ms = (process_seconds()-cpu_start)*1000;
  double wall_ms = std::chrono::duration<double,std::milli>(std::chrono::steady_clock::now()-wall_start).count();
  float gpu_ms; CUDA(cudaEventElapsedTime(&gpu_ms, start, stop));
  State result{}; CUDA(cudaMemcpy(&result, state.p, sizeof(result), cudaMemcpyDeviceToHost));
  std::vector<Object> object_result(p.roots.size());
  CUDA(cudaMemcpy(object_result.data(), objects.p, object_result.size()*sizeof(Object), cudaMemcpyDeviceToHost));
  bool valid = !result.error && result.completed_tasks == p.tasks.size()*repeats &&
               result.completed_runs == static_cast<unsigned long long>(repeats);
  double max_error = 0;
  if (!result.error) {
    for (size_t i = 0; i < object_result.size(); ++i) {
      const auto& o = object_result[i];
      valid &= o.pins == 0 && o.remaining == 0;
      valid &= i == size_t(p.output) ? (o.phase == Published && o.roots == 1 && o.slot >= 0) : o.phase == Retired;
    }
    valid &= result.occupied == 1 && result.peak_slots <= slots;
    valid &= result.publications == p.tasks.size()*repeats && result.retirements == p.tasks.size()*repeats;
    if (object_result[p.output].slot < 0) throw std::runtime_error("missing output backing");
    std::vector<float> actual(p.seed.size());
    CUDA(cudaMemcpy(actual.data(), pool.p+size_t(object_result[p.output].slot)*v.elements,
                    actual.size()*sizeof(float), cudaMemcpyDeviceToHost));
    auto expected = reference(p, kind);
    for (size_t i = 0; i < actual.size(); ++i) {
      double e = std::abs(double(actual[i])-expected[i]);
      max_error = std::max(max_error, e);
      valid &= std::isfinite(actual[i]) && e <= 2e-5 + 2e-5*std::abs(expected[i]);
    }
    if (!output.empty()) {
      std::ofstream f(output, std::ios::binary);
      f.write(reinterpret_cast<const char*>(actual.data()), actual.size()*sizeof(float));
      if (!f) throw std::runtime_error("output write failed");
    }
  }
  std::cout << std::setprecision(10)
    << "{\"schema\":1,\"device\":\"" << props.name << "\",\"compute_capability\":\"" << props.major << "." << props.minor
    << "\",\"cuda_runtime\":" << runtime_version << ",\"cuda_driver\":" << driver_version
    << ",\"mode\":\"" << mode << "\",\"workload\":\"" << kind << "\",\"n\":" << n
    << ",\"tasks_per_run\":" << p.tasks.size() << ",\"repeats\":" << repeats << ",\"blocks\":" << blocks
    << ",\"gpu_ms\":" << gpu_ms << ",\"wall_ms\":" << wall_ms << ",\"process_cpu_ms\":" << cpu_ms
    << ",\"host_submission_calls\":" << (mode == "host" ? (p.tasks.size()+2)*repeats : 1)
    << ",\"completed_tasks\":" << result.completed_tasks << ",\"completed_runs\":" << result.completed_runs
    << ",\"publications\":" << result.publications << ",\"retirements\":" << result.retirements
    << ",\"peak_slots\":" << result.peak_slots << ",\"pool_bytes\":" << size_t(slots)*v.elements*sizeof(float)
    << ",\"max_abs_error\":" << max_error << ",\"error_code\":" << result.error
    << ",\"valid\":" << (valid ? "true" : "false") << "}\n";
  if (executable) CUDA(cudaGraphExecDestroy(executable));
  if (graph) CUDA(cudaGraphDestroy(graph));
  CUDA(cudaEventDestroy(start)); CUDA(cudaEventDestroy(stop)); CUDA(cudaStreamDestroy(stream));
  return valid ? 0 : 2;
} catch (const std::exception& e) {
  std::cerr << "damer_gpu: " << e.what() << '\n';
  return 1;
}
