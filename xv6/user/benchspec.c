#include "kernel/types.h"
#include "kernel/stat.h"
#include "user/user.h"
#include "kernel/fcntl.h"

static char block[1024];

int
benchmark_write(int size_kb, int speculative)
{
  char filename[16];
  if (speculative)
    strcpy(filename, "bench_s");
  else
    strcpy(filename, "bench_n");

  unlink(filename);

  if (speculative) {
    if (xbegin() < 0)
      return -2;
  }

  int t0 = uptime();

  int fd = open(filename, O_CREATE | O_WRONLY);
  if (fd < 0) {
    if (speculative) xabort();
    return -1;
  }

  int failed = 0;
  for (int i = 0; i < size_kb; i++) {
    if (write(fd, block, 1024) != 1024) {
      failed = 1;
      break;
    }
  }

  close(fd);

  if (speculative) {
    if (failed) {
      xabort();
      return -1; // Cliff reached!
    }
    if (xcommit() < 0) {
      return -3;
    }
  }

  int t1 = uptime();
  unlink(filename);

  return (t1 - t0);
}

int
main(int argc, char *argv[])
{
  memset(block, 'A', sizeof(block));

  printf("\n===============================================================\n");
  printf("  BENCHMARK: Write Cost Inside vs Outside Speculation\n");
  printf("  Evaluating file sizes 1 KB to 68 KB (NSHADOW = 64)\n");
  printf("===============================================================\n");
  printf("Size_KB | Outside_Ticks | Inside_Ticks | Status\n");
  printf("--------+---------------+--------------+-----------------------\n");

  int sizes[] = {1, 2, 4, 8, 16, 24, 32, 40, 48, 56, 60, 62, 64, 68};
  int n_sizes = sizeof(sizes) / sizeof(sizes[0]);

  for (int i = 0; i < n_sizes; i++) {
    int sz = sizes[i];
    int t_out = benchmark_write(sz, 0);
    int t_in = benchmark_write(sz, 1);

    if (t_in >= 0) {
      printf("%d KB\t|\t%d ticks\t|\t%d ticks\t|\tSUCCESS\n", sz, t_out, t_in);
    } else {
      printf("%d KB\t|\t%d ticks\t|\tCLIFF (exceeded %d)\t|\tEXHAUSTION (Returned -1)\n", sz, t_out, sz);
    }
  }
  printf("===============================================================\n");
  printf("Benchmark complete. Notice the sharp cliff when exceeding 64 KB.\n\n");
  exit(0);
}
