// ABI bridge to upstream fast_exp SpinLock. The shared region is supplied by
// the caller and verified on the real CXL node; no emulator is linked.
#include <atomic>
#include <cerrno>
#include <cstdint>
#include <cstdlib>
#include <new>
#include <stdexcept>
#include <time.h>
#include "lib/lock/spin_lock.hpp"

// Upstream's -Dcxl selects its region-backed implementation. Allocation and
// destruction through that legacy allocator API must never occur here.
extern "C" void *emucxl_alloc(size_t, int) { std::abort(); }
extern "C" void emucxl_free(void *, size_t) { std::abort(); }
extern "C" void emucxl_init() { std::abort(); }
extern "C" void emucxl_exit() { std::abort(); }

static uint64_t now_ns() {
    timespec t;
    clock_gettime(CLOCK_MONOTONIC, &t);
    return uint64_t(t.tv_sec) * 1000000000ULL + t.tv_nsec;
}

extern "C" size_t cxl_lock_bytes() { return SpinLock::get_cxl_region_size(1); }
extern "C" void cxl_lock_init(void *region) {
    RegionLayout layout;
    auto handle = layout.reserve<std::atomic_flag>();
    auto *flag = RegionLayout::resolve(handle, static_cast<volatile char *>(region));
    new (flag) std::atomic_flag{};
    flag->clear(std::memory_order_release);
}
extern "C" void *cxl_lock_attach(void *region) {
    auto *lock = new (std::nothrow) SpinLock;
    if (lock) lock->region_init(1, static_cast<volatile char *>(region));
    return lock;
}
extern "C" int cxl_lock_acquire(void *handle, uint64_t timeout_ns,
                                  uint64_t *failed_attempts, uint64_t *wait_ns) {
    auto *lock = static_cast<SpinLock *>(handle);
    uint64_t begin = now_ns(), failed = 0;
    while (!lock->trylock(0)) {
        ++failed;
        if ((failed & 63) == 0) {
            if (now_ns() - begin >= timeout_ns) {
                *failed_attempts = failed;
                *wait_ns = now_ns() - begin;
                return ETIMEDOUT;
            }
            sched_yield();
        } else {
            LockSpinHint();
        }
    }
    *failed_attempts = failed;
    *wait_ns = now_ns() - begin;
    return 0;
}
extern "C" void cxl_lock_release(void *handle) {
    static_cast<SpinLock *>(handle)->unlock(0);
}
extern "C" void cxl_lock_detach(void *handle) {
    // Do not call upstream destroy(): the caller owns the shared mapping.
    delete static_cast<SpinLock *>(handle);
}
