#define SEC(NAME) __attribute__((section(NAME), used))

SEC("tracepoint/syscalls/sys_enter_nanosleep")
int damer_kernel_smoke(void *ctx) {
  return 0;
}

char LICENSE[] SEC("license") = "GPL";
