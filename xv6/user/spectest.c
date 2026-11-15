#include "kernel/types.h"
#include "kernel/stat.h"
#include "user/user.h"
#include "kernel/fcntl.h"

void
test_abort(void)
{
  printf("=== Test 1: Abort Isolation (rm a, echo hi > b) ===\n");

  // Setup initial state: create file 'a' with "original_a"
  unlink("a");
  unlink("b");
  int fd = open("a", O_CREATE | O_WRONLY);
  if (fd < 0) {
    printf("FAILED: cannot create a\n");
    exit(1);
  }
  write(fd, "original_a", 10);
  close(fd);

  int pid = fork();
  if (pid < 0) {
    printf("FAILED: fork failed\n");
    exit(1);
  }

  if (pid == 0) {
    // Child enters speculation
    if (xbegin() < 0) {
      printf("CHILD FAILED: xbegin error\n");
      exit(1);
    }

    // Speculatively delete 'a'
    if (unlink("a") < 0) {
      printf("CHILD FAILED: unlink a failed\n");
      exit(1);
    }

    // Verify 'a' appears deleted to the speculative child
    int tfd = open("a", O_RDONLY);
    if (tfd >= 0) {
      printf("CHILD FAILED: a still visible to child\n");
      close(tfd);
      exit(1);
    }

    // Speculatively create 'b' and write "hi"
    fd = open("b", O_CREATE | O_WRONLY);
    if (fd < 0) {
      printf("CHILD FAILED: cannot create b\n");
      exit(1);
    }
    write(fd, "hi\n", 3);
    close(fd);

    // Speculative print: MUST be muzzled and never reach UART!
    printf("CHILD: this message should NEVER appear on console!\n");

    // Abort the speculation!
    if (xabort() < 0) {
      printf("CHILD FAILED: xabort failed\n");
      exit(1);
    }
    exit(0);
  }

  wait(0);

  // Parent outside checks:
  // 1. File 'a' MUST still exist with original content
  fd = open("a", O_RDONLY);
  if (fd < 0) {
    printf("FAILED: file 'a' was deleted externally!\n");
    exit(1);
  }
  char buf[32];
  memset(buf, 0, sizeof(buf));
  read(fd, buf, 10);
  close(fd);
  if (strcmp(buf, "original_a") != 0) {
    printf("FAILED: file 'a' content corrupted: %s\n", buf);
    exit(1);
  }

  // 2. File 'b' MUST NOT exist
  fd = open("b", O_RDONLY);
  if (fd >= 0) {
    printf("FAILED: file 'b' exists on real disk after abort!\n");
    close(fd);
    exit(1);
  }

  printf("PASSED: Abort left zero filesystem and console trace.\n\n");
}

void
test_commit(void)
{
  printf("=== Test 2: Commit Atomicity (rm a, echo hi > b) ===\n");

  unlink("a");
  unlink("b");
  int fd = open("a", O_CREATE | O_WRONLY);
  if (fd < 0) {
    printf("FAILED: cannot create a\n");
    exit(1);
  }
  write(fd, "original_a", 10);
  close(fd);

  int pid = fork();
  if (pid < 0) {
    printf("FAILED: fork failed\n");
    exit(1);
  }

  if (pid == 0) {
    if (xbegin() < 0) {
      printf("CHILD FAILED: xbegin error\n");
      exit(1);
    }

    if (unlink("a") < 0) {
      printf("CHILD FAILED: unlink a failed\n");
      exit(1);
    }

    fd = open("b", O_CREATE | O_WRONLY);
    if (fd < 0) {
      printf("CHILD FAILED: cannot create b\n");
      exit(1);
    }
    write(fd, "hi\n", 3);
    close(fd);

    printf("CHILD: commit releasing held console output!\n");

    if (xcommit() < 0) {
      printf("CHILD FAILED: xcommit failed\n");
      exit(1);
    }
    exit(0);
  }

  wait(0);

  // Outside checks:
  // 1. 'a' must be deleted
  fd = open("a", O_RDONLY);
  if (fd >= 0) {
    printf("FAILED: file 'a' still exists after commit!\n");
    close(fd);
    exit(1);
  }

  // 2. 'b' must exist with "hi\n"
  fd = open("b", O_RDONLY);
  if (fd < 0) {
    printf("FAILED: file 'b' does not exist after commit!\n");
    exit(1);
  }
  char buf[32];
  memset(buf, 0, sizeof(buf));
  read(fd, buf, 3);
  close(fd);
  if (strcmp(buf, "hi\n") != 0) {
    printf("FAILED: file 'b' wrong content: %s\n", buf);
    exit(1);
  }

  unlink("b");
  printf("PASSED: Commit successfully applied both effects atomically.\n\n");
}

void
test_fork_inheritance(void)
{
  printf("=== Test 3: Process Tree fork() Speculation Inheritance ===\n");

  unlink("tree_test");
  if (xbegin() < 0) {
    printf("FAILED: xbegin\n");
    exit(1);
  }

  int pid = fork();
  if (pid < 0) {
    printf("FAILED: fork\n");
    exit(1);
  }

  if (pid == 0) {
    // Child is inside same speculation! Writes to file
    int fd = open("tree_test", O_CREATE | O_WRONLY);
    if (fd < 0) {
      printf("CHILD FAILED: create tree_test\n");
      exit(1);
    }
    write(fd, "child_work", 10);
    close(fd);
    exit(0);
  }

  wait(0);

  // Parent reads child's speculative write
  int fd = open("tree_test", O_RDONLY);
  if (fd < 0) {
    printf("FAILED: parent cannot see child's speculative write\n");
    xabort();
    exit(1);
  }
  char buf[32];
  memset(buf, 0, sizeof(buf));
  read(fd, buf, 10);
  close(fd);

  if (strcmp(buf, "child_work") != 0) {
    printf("FAILED: wrong content in tree_test: %s\n", buf);
    xabort();
    exit(1);
  }

  // Abort
  xabort();

  // Outside verify: tree_test must not exist
  fd = open("tree_test", O_RDONLY);
  if (fd >= 0) {
    printf("FAILED: tree_test exists after abort!\n");
    close(fd);
    exit(1);
  }

  printf("PASSED: Process tree inheritance and shared speculation verified.\n\n");
}

int
main(int argc, char *argv[])
{
  printf("\n=== RUNNING SPECULATION TEST SUITE (spectest) ===\n\n");
  test_abort();
  test_commit();
  test_fork_inheritance();
  printf("ALL SPECTEST CASES PASSED SUCCESSFULLY!\n");
  exit(0);
}
