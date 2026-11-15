#include "kernel/types.h"
#include "kernel/stat.h"
#include "user/user.h"
#include "kernel/fcntl.h"

void
test_nested_xbegin(void)
{
  printf("=== Hidden Test 1: Nested xbegin() Handling ===\n");
  if (xbegin() < 0) {
    printf("FAILED: initial xbegin\n");
    exit(1);
  }

  // Second nested call must be rejected
  int r = xbegin();
  if (r == 0) {
    printf("FAILED: nested xbegin succeeded unexpectedly!\n");
    xabort();
    exit(1);
  }

  printf("PASSED: nested xbegin correctly rejected with %d.\n", r);
  xabort();
}

void
test_shadow_exhaustion(void)
{
  printf("\n=== Hidden Test 2: Shadow Table Exhaustion (NSHADOW=64) ===\n");
  unlink("exhaust_test");

  if (xbegin() < 0) {
    printf("FAILED: xbegin\n");
    exit(1);
  }

  int fd = open("exhaust_test", O_CREATE | O_WRONLY);
  if (fd < 0) {
    printf("FAILED: create exhaust_test\n");
    xabort();
    exit(1);
  }

  char block[1024];
  memset(block, 'X', sizeof(block));

  int failed = 0;
  int written_blocks = 0;

  // Try to write 70 blocks (70 KB), which exceeds NSHADOW (64)
  for (int i = 0; i < 70; i++) {
    int w = write(fd, block, 1024);
    if (w < 0) {
      failed = 1;
      written_blocks = i;
      break;
    }
  }

  close(fd);

  if (!failed) {
    printf("FAILED: wrote >64 blocks without shadow table exhaustion!\n");
    xabort();
    exit(1);
  }

  printf("PASSED: write failed safely with -1 after block %d (Cliff reached).\n", written_blocks);
  printf("PASSED: Non-externalization guarantee preserved (no disk fallback).\n");

  xabort();

  // Verify real disk: exhaust_test must not exist
  fd = open("exhaust_test", O_RDONLY);
  if (fd >= 0) {
    printf("FAILED: exhaust_test leaked onto real disk after abort!\n");
    close(fd);
    exit(1);
  }
  printf("PASSED: Real disk remained completely untouched.\n");
}

int
main(int argc, char *argv[])
{
  printf("\n=== RUNNING HIDDEN TESTS (hiddentest) ===\n\n");
  test_nested_xbegin();
  test_shadow_exhaustion();
  printf("\nALL HIDDEN TESTS PASSED!\n");
  exit(0);
}
