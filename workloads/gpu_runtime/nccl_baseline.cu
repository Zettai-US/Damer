// Optional transport baseline. This is not an equivalent scientific workload
// and must never be divided into damer_gpu timings to claim a speedup.
#include <cuda_runtime.h>
#include <nccl.h>
#if NCCL_VERSION_CODE >= 22900
#include <nccl_device.h>
#endif
#include <mpi.h>
#include <algorithm>
#include <chrono>
#include <cmath>
#include <ctime>
#include <iostream>
#include <stdexcept>
#include <string>
#include <vector>

#define CUDA(c) do { auto e=(c); if(e!=cudaSuccess) throw std::runtime_error(cudaGetErrorString(e)); } while(0)
#define NCCL(c) do { auto e=(c); if(e!=ncclSuccess) throw std::runtime_error(ncclGetErrorString(e)); } while(0)
#define MPI_CHECK(c) do { if((c)!=MPI_SUCCESS) throw std::runtime_error("MPI failure"); } while(0)

double cpu_seconds() { timespec t{}; clock_gettime(CLOCK_PROCESS_CPUTIME_ID,&t); return t.tv_sec+t.tv_nsec*1.e-9; }

int main(int argc, char** argv) {
  MPI_Init(&argc, &argv);
  try {
    int rank, ranks, local_rank, local_size;
    MPI_CHECK(MPI_Comm_rank(MPI_COMM_WORLD, &rank));
    MPI_CHECK(MPI_Comm_size(MPI_COMM_WORLD, &ranks));
    MPI_Comm local;
    MPI_CHECK(MPI_Comm_split_type(MPI_COMM_WORLD, MPI_COMM_TYPE_SHARED, rank, MPI_INFO_NULL, &local));
    MPI_CHECK(MPI_Comm_rank(local, &local_rank)); MPI_CHECK(MPI_Comm_size(local, &local_size));
    int count = 262144, repeats = 100;
    std::string mode = "host";
    for (int i=1; i<argc; ++i) {
      std::string key=argv[i];
      if (++i==argc) throw std::runtime_error("missing option value");
      std::string value=argv[i];
      if(key=="--mode") mode=value;
      else {
        size_t used; int number=std::stoi(value,&used);
        if(used!=value.size()) throw std::runtime_error("invalid integer");
        if(key=="--count") count=number;
        else if(key=="--repeats") repeats=number;
        else throw std::runtime_error("unknown option");
      }
    }
    if(count<1 || count>(1<<26) || repeats<1 || repeats>100000 || (mode!="host" && mode!="graph"))
      throw std::runtime_error("invalid workload");
    if(ranks<2) throw std::runtime_error("at least two GPU ranks required; one-rank allreduce is not a network test");
    CUDA(cudaSetDeviceFlags(cudaDeviceScheduleBlockingSync));
    int devices; CUDA(cudaGetDeviceCount(&devices));
    if(local_size>devices) throw std::runtime_error("requires one visible GPU per local rank");
    CUDA(cudaSetDevice(local_rank));
    cudaDeviceProp device{}; CUDA(cudaGetDeviceProperties(&device,local_rank));
    ncclUniqueId id;
    if(rank==0) NCCL(ncclGetUniqueId(&id));
    MPI_CHECK(MPI_Bcast(&id,sizeof(id),MPI_BYTE,0,MPI_COMM_WORLD));
    ncclComm_t comm; NCCL(ncclCommInitRank(&comm,ranks,id,rank));
    int version; NCCL(ncclGetVersion(&version));
    // Capability advertisement is not a completed GIN transfer. Never silently
    // label this host AllReduce as GIN, even when NCCL internally uses it.
    int gin_proxy=-1, gin_gdaki=-1, device_api=-1;
#if NCCL_VERSION_CODE >= 22900
    ncclCommProperties_t props=NCCL_COMM_PROPERTIES_INITIALIZER;
    NCCL(ncclCommQueryProperties(comm,&props));
    device_api=props.deviceApiSupport;
#if NCCL_VERSION_CODE >= 23100
    gin_proxy=props.ginSupport[NCCL_GIN_TYPE_PROXY];
    gin_gdaki=props.ginSupport[NCCL_GIN_TYPE_GDAKI];
#else
    gin_proxy=props.ginType==NCCL_GIN_TYPE_PROXY;
    gin_gdaki=props.ginType==NCCL_GIN_TYPE_GDAKI;
#endif
#endif
    float *input, *output;
    CUDA(cudaMalloc(&input,size_t(count)*sizeof(float)));
    CUDA(cudaMalloc(&output,size_t(count)*sizeof(float)));
    std::vector<float> data(count,float(rank+1));
    CUDA(cudaMemcpy(input,data.data(),size_t(count)*sizeof(float),cudaMemcpyHostToDevice));
    cudaStream_t stream; CUDA(cudaStreamCreateWithFlags(&stream,cudaStreamNonBlocking));
    for(int i=0;i<5;++i) NCCL(ncclAllReduce(input,output,count,ncclFloat,ncclSum,comm,stream));
    CUDA(cudaStreamSynchronize(stream));
    cudaGraph_t graph=nullptr; cudaGraphExec_t executable=nullptr;
    if(mode=="graph") {
      CUDA(cudaStreamBeginCapture(stream,cudaStreamCaptureModeThreadLocal));
      for(int i=0;i<repeats;++i) NCCL(ncclAllReduce(input,output,count,ncclFloat,ncclSum,comm,stream));
      CUDA(cudaStreamEndCapture(stream,&graph)); CUDA(cudaGraphInstantiate(&executable,graph,0));
      CUDA(cudaGraphUpload(executable,stream)); CUDA(cudaStreamSynchronize(stream));
    }
    cudaEvent_t start,stop; CUDA(cudaEventCreate(&start)); CUDA(cudaEventCreateWithFlags(&stop,cudaEventBlockingSync));
    MPI_CHECK(MPI_Barrier(MPI_COMM_WORLD));
    double cpu_start=cpu_seconds(); auto wall_start=std::chrono::steady_clock::now();
    CUDA(cudaEventRecord(start,stream));
    if(mode=="graph") CUDA(cudaGraphLaunch(executable,stream));
    else for(int i=0;i<repeats;++i) NCCL(ncclAllReduce(input,output,count,ncclFloat,ncclSum,comm,stream));
    CUDA(cudaEventRecord(stop,stream)); CUDA(cudaEventSynchronize(stop));
    double cpu_ms=(cpu_seconds()-cpu_start)*1000;
    double wall_ms=std::chrono::duration<double,std::milli>(std::chrono::steady_clock::now()-wall_start).count();
    float gpu_ms; CUDA(cudaEventElapsedTime(&gpu_ms,start,stop));
    CUDA(cudaMemcpy(data.data(),output,size_t(count)*sizeof(float),cudaMemcpyDeviceToHost));
    float expected=float(double(ranks)*(ranks+1)/2);
    int valid=1;
    for(float x:data) if(!std::isfinite(x) || std::abs(x-expected)>1.e-5f*expected) valid=0;
    int all_valid; MPI_CHECK(MPI_Allreduce(&valid,&all_valid,1,MPI_INT,MPI_MIN,MPI_COMM_WORLD));
    // Serialize output only after timing; retain every rank's CPU time.
    for(int r=0;r<ranks;++r) {
      MPI_CHECK(MPI_Barrier(MPI_COMM_WORLD));
      if(rank==r) std::cout << "{\"schema\":1,\"baseline\":\"nccl-allreduce-" << mode
        << "\",\"rank\":" << rank << ",\"ranks\":" << ranks << ",\"device\":\"" << device.name
        << "\",\"nccl_version\":" << version << ",\"bytes_per_rank\":" << size_t(count)*4
        << ",\"repeats\":" << repeats << ",\"gpu_ms\":" << gpu_ms << ",\"wall_ms\":" << wall_ms
        << ",\"process_cpu_ms\":" << cpu_ms << ",\"host_submission_calls\":" << (mode=="graph"?1:repeats)
        << ",\"device_api_advertised\":" << device_api << ",\"gin_proxy_advertised\":" << gin_proxy
        << ",\"gin_gdaki_advertised\":" << gin_gdaki
        << ",\"gin_transfer_tested\":false,\"valid\":" << (all_valid?"true":"false") << "}" << std::endl;
    }
    if(executable) CUDA(cudaGraphExecDestroy(executable));
    if(graph) CUDA(cudaGraphDestroy(graph));
    CUDA(cudaEventDestroy(start)); CUDA(cudaEventDestroy(stop)); CUDA(cudaStreamDestroy(stream));
    CUDA(cudaFree(input)); CUDA(cudaFree(output)); NCCL(ncclCommDestroy(comm));
    MPI_CHECK(MPI_Comm_free(&local)); MPI_Finalize(); return all_valid?0:2;
  } catch(const std::exception& e) {
    std::cerr << "damer_nccl: " << e.what() << std::endl;
    MPI_Abort(MPI_COMM_WORLD,1); return 1;
  }
}
