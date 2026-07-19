#include "damer/bpftime_frontend.h"

SEC("damer/switchlet")
int kv_pack(struct damer_bpf_ctx *ctx) {
  struct damer_edge_event edge = {
      .source_node = DAMER_NODE_CXL_MEMORY_DEVICE,
      .destination_node = DAMER_NODE_CXL_MEMORY_DEVICE,
      .transformations = DAMER_TRANSFORM_QUANTIZE,
      .ordering = DAMER_ORDER_PROGRAM,
      .ownership = DAMER_OWNERSHIP_DESTINATION,
      .alias_set = 1,
      .bytes = 0,
      .stride = 1,
      .reuse_distance = 0,
      .read_ratio = 1,
      .write_ratio = 1,
      .source_addr = 0,
      .destination_addr = 0,
  };

  return damer_emit_edge(ctx, &edge);
}

char LICENSE[] SEC("license") = "Dual BSD/GPL";
