#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <linux/mempolicy.h>
#include <pthread.h>
#include <sched.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/prctl.h>
#include <sys/syscall.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>
#include <x86intrin.h>

#define MAX_W 8
#define MiB (1024UL*1024)
static const int cpus[MAX_W]={2,8,14,20,3,9,15,21};
static uint64_t now_ns(void){struct timespec t;clock_gettime(CLOCK_MONOTONIC,&t);return (uint64_t)t.tv_sec*1000000000+t.tv_nsec;}
static void die(const char *s){perror(s);exit(2);}
static void pin(int cpu){cpu_set_t s;CPU_ZERO(&s);CPU_SET(cpu,&s);if(sched_setaffinity(0,sizeof(s),&s))die("affinity");}
static void bind(void *p,size_t n,int node){unsigned long mask=1UL<<node;if(syscall(SYS_mbind,p,n,MPOL_BIND,&mask,8*sizeof(mask),0))die("mbind");}
static void *local_alloc(size_t n){void *p=mmap(NULL,n,PROT_READ|PROT_WRITE,MAP_PRIVATE|MAP_ANONYMOUS,-1,0);if(p==MAP_FAILED)die("mmap local");bind(p,n,0);madvise(p,n,MADV_NOHUGEPAGE);return p;}
static void page_nodes(void *p,long *n0,long *n1){
  FILE *f=fopen("/proc/self/numa_maps","r");if(!f)die("numa_maps");char *line=NULL;size_t cap=0;*n0=*n1=0;
  while(getline(&line,&cap,f)>0){unsigned long a=0;sscanf(line,"%lx",&a);if(a==(unsigned long)p){char *q=strstr(line," N0=");if(q)*n0=strtol(q+4,NULL,10);q=strstr(line," N1=");if(q)*n1=strtol(q+4,NULL,10);break;}}
  free(line);fclose(f);
}
static long mapping_pss(pid_t pid,uintptr_t addr){
  char name[80];snprintf(name,sizeof(name),"/proc/%d/smaps",pid);FILE *f=fopen(name,"r");if(!f)die("smaps");
  char *line=NULL;size_t cap=0;int active=0;long value=0;
  while(getline(&line,&cap,f)>0){unsigned long lo,hi;if(sscanf(line,"%lx-%lx",&lo,&hi)==2){active=(addr>=lo&&addr<hi);}else if(active&&sscanf(line,"Pss: %ld kB",&value)==1)break;}
  free(line);fclose(f);return value;
}
__attribute__((noinline)) static uint64_t scan(const uint64_t *p,size_t bytes){
  asm volatile("" ::: "memory");uint64_t s=0;for(size_t i=0;i<bytes/8;i++)s+=p[i];return s;
}
static void flush(const void *p,size_t n){for(size_t i=0;i<n;i+=64)_mm_clflush((const char*)p+i);_mm_mfence();}
static int pool_create(size_t size,void **view){
  int fd=memfd_create("arvm_cxl_pool",MFD_ALLOW_SEALING);if(fd<0)die("memfd");if(ftruncate(fd,size))die("truncate");
  uint64_t *p=mmap(NULL,size,PROT_READ|PROT_WRITE,MAP_SHARED,fd,0);if(p==MAP_FAILED)die("pool mmap");bind(p,size,1);madvise(p,size,MADV_NOHUGEPAGE);
  for(size_t i=0;i<size/8;i++)p[i]=(uint64_t)i*6364136223846793005ULL+1442695040888963407ULL;
  long n0,n1;page_nodes(p,&n0,&n1);if(n0!=0||n1!=(long)(size/4096)){fprintf(stderr,"Pool placement failed N0=%ld N1=%ld expected=%zu\n",n0,n1,size/4096);exit(3);}
  munmap(p,size);if(fcntl(fd,F_ADD_SEALS,F_SEAL_WRITE|F_SEAL_GROW|F_SEAL_SHRINK|F_SEAL_SEAL))die("seal");
  *view=mmap(NULL,size,PROT_READ,MAP_SHARED,fd,0);if(*view==MAP_FAILED)die("pool remap");scan(*view,size);return fd;
}
struct slot {uint64_t end,checksum;long n0,n1;uintptr_t addr;};
struct ctl {pthread_barrier_t start,done,release;uint64_t start_ns;struct slot slots[MAX_W];};
static void fanout(int n,size_t size,int scans,int reps,int copy){
  void *source;int fd=pool_create(size,&source);uint64_t expected=scan(source,size)*(uint64_t)scans;
  struct ctl *c=mmap(NULL,sizeof(*c),PROT_READ|PROT_WRITE,MAP_SHARED|MAP_ANONYMOUS,-1,0);if(c==MAP_FAILED)die("control mmap");
  pthread_barrierattr_t a;pthread_barrierattr_init(&a);pthread_barrierattr_setpshared(&a,PTHREAD_PROCESS_SHARED);
  pthread_barrier_init(&c->start,&a,n+1);pthread_barrier_init(&c->done,&a,n+1);pthread_barrier_init(&c->release,&a,n+1);
  pid_t children[MAX_W];
  for(int w=0;w<n;w++){
    pid_t pid=fork();if(pid<0)die("fork");if(!pid){pin(cpus[w]);munmap(source,size);
      void *retained=NULL;if(copy==2){retained=local_alloc(size);memset(retained,0,size);}
      for(int r=-2;r<reps;r++){
        pthread_barrier_wait(&c->start);
        void *mapped=mmap(NULL,size,PROT_READ,MAP_SHARED,fd,0);if(mapped==MAP_FAILED)die("worker mmap");void *input=mapped;
        if(copy){input=copy==2?retained:local_alloc(size);memcpy(input,mapped,size);munmap(mapped,size);}
        uint64_t s=0;for(int j=0;j<scans;j++)s+=scan(input,size);
        c->slots[w].end=now_ns();c->slots[w].checksum=s;c->slots[w].addr=(uintptr_t)input;
        page_nodes(input,&c->slots[w].n0,&c->slots[w].n1);
        pthread_barrier_wait(&c->done);pthread_barrier_wait(&c->release);if(copy!=2)munmap(input,size);
      }if(retained)munmap(retained,size);close(fd);_exit(0);
    }children[w]=pid;
  }
  pin(23);
  for(int r=-2;r<reps;r++){
    flush(source,size);c->start_ns=now_ns();pthread_barrier_wait(&c->start);pthread_barrier_wait(&c->done);
    uint64_t end=0;long n0=0,n1=0,pss=0;
    for(int w=0;w<n;w++){if(c->slots[w].checksum!=expected){fprintf(stderr,"checksum mismatch\n");exit(4);}if(c->slots[w].end>end)end=c->slots[w].end;n0+=c->slots[w].n0;n1+=c->slots[w].n1;if(r==0)pss+=mapping_pss(children[w],c->slots[w].addr);}
    if(copy&&(n0!=(long)(n*size/4096)||n1!=0)){fprintf(stderr,"DRAM placement mismatch N0=%ld N1=%ld\n",n0,n1);exit(5);}
    if(!copy&&(n0!=0||n1!=(long)(n*size/4096))){fprintf(stderr,"shared placement mismatch\n");exit(6);}
    if(r==0&&!copy)pss+=mapping_pss(getpid(),(uintptr_t)source);
    if(r>=0)printf("{\"suite\":\"fanout\",\"policy\":\"%s\",\"workers\":%d,\"bytes\":%zu,\"scans\":%d,\"iteration\":%d,\"duration_ns\":%lu,\"mapped_pages_node0\":%ld,\"mapped_pages_node1\":%ld,\"payload_pss_kib\":%ld,\"checksum_ok\":true}\n",copy==2?"copy-reuse":copy?"copy":"share",n,size,scans,r,end-c->start_ns,n0,n1,r==0?pss:-1L);
    fflush(stdout);pthread_barrier_wait(&c->release);
  }
  for(int w=0;w<n;w++){int s;waitpid(children[w],&s,0);if(s)exit(7);}munmap(source,size);close(fd);munmap(c,sizeof(*c));
}
static void sleep_until(uint64_t deadline){struct timespec t={deadline/1000000000,deadline%1000000000};int r;while((r=clock_nanosleep(CLOCK_MONOTONIC,TIMER_ABSTIME,&t,NULL))==EINTR);if(r){errno=r;die("sleep");}}
/* A phase-replay kernel. The next object ID is supplied by an accepted plan;
   uncertain hints can be wrong, but exact IDs are checked before consumption.
   A single reusable local slot bounds DRAM usage, independent of phase count. */
static void phases(size_t size,int reps,int steps,int lookahead,int correct_pct,int gap_us){
  void *source;int fd=pool_create(size*4,&source);uint64_t expected[4];for(int i=0;i<4;i++)expected[i]=scan((const uint64_t*)((char*)source+i*size),size);
  pin(2);void *local=local_alloc(size);memset(local,0,size);long n0,n1;page_nodes(local,&n0,&n1);if(n0!=(long)(size/4096)||n1)exit(8);
  for(int r=-2;r<reps;r++){
    flush(source,size*4);uint64_t total=0,copy_ns=0;size_t copies=0;int hits=0;
    for(int j=0;j<steps;j++){
      int actual=(j+r+reps*4)%4;
      /* Exact deterministic fraction for 20 steps, rotated across repetitions. */
      int hit=((j*5+r+reps*4)%100)<correct_pct;
      int hinted=hit?actual:(actual+1)%4;
      uint64_t begin=now_ns();int resident=-1;
      if(lookahead){uint64_t b=now_ns();memcpy(local,(char*)source+hinted*size,size);copy_ns+=now_ns()-b;copies++;resident=hinted;}
      sleep_until(begin+(uint64_t)gap_us*1000);
      if(resident!=actual){uint64_t b=now_ns();memcpy(local,(char*)source+actual*size,size);copy_ns+=now_ns()-b;copies++;}
      else hits++;
      uint64_t sum=scan(local,size);if(sum!=expected[actual]){fprintf(stderr,"phase checksum mismatch\n");exit(9);}total+=now_ns()-begin;
    }
    if(r>=0)printf("{\"suite\":\"phases\",\"policy\":\"%s\",\"bytes\":%zu,\"steps\":%d,\"iteration\":%d,\"gap_us\":%d,\"hint_accuracy_pct\":%d,\"hint_hits\":%d,\"duration_ns\":%lu,\"copy_ns\":%lu,\"bytes_copied\":%zu,\"local_pages_node0\":%ld,\"checksum_ok\":true}\n",lookahead?"lookahead":"demand",size,steps,r,gap_us,correct_pct,hits,total,copy_ns,copies*size,n0);
    fflush(stdout);
  }
  munmap(local,size);munmap(source,size*4);close(fd);
}
int main(int argc,char **argv){
  alarm(240);
  if(argc==8&&!strcmp(argv[1],"fanout")){int n=atoi(argv[2]);if(n<1||n>8)return 1;int mode=!strcmp(argv[6],"copy-reuse")?2:!strcmp(argv[6],"copy");fanout(n,strtoul(argv[3],NULL,10)*MiB,atoi(argv[4]),atoi(argv[5]),mode);return 0;}
  if(argc==9&&!strcmp(argv[1],"phases")){phases(strtoul(argv[2],NULL,10)*MiB,atoi(argv[3]),atoi(argv[4]),!strcmp(argv[5],"lookahead"),atoi(argv[6]),atoi(argv[7]));return 0;}
  fprintf(stderr,"usage: %s fanout N MiB scans reps copy|share run-id\n       %s phases MiB reps steps demand|lookahead accuracy gap-us run-id\n",argv[0],argv[0]);return 1;
}
