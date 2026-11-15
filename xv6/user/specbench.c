// Guest-tick measurements and an ordinary-user-program crash-recovery fixture.
#include "kernel/types.h"
#include "kernel/fcntl.h"
#include "user/user.h"

static char buffer[1024];

static int
put(char *path, char *data)
{
  int fd = open(path, O_CREATE | O_TRUNC | O_WRONLY);
  if (fd < 0) return -1;
  int n = write(fd, data, strlen(data));
  close(fd);
  return n == strlen(data) ? 0 : -1;
}

int
main(int argc, char **argv)
{
  if (argc == 2) {
    if (strcmp(argv[1], "prepare") == 0) {
      if (put("crasha", "old-a") || put("crashb", "old-b")) exit(1);
      sync();
      printf("CRASH BASELINE READY\n");
      exit(0);
    }
    if (strcmp(argv[1], "commit") == 0) {
      if (xbegin() || put("crasha", "new-a") || put("crashb", "new-b") || xcommit()) exit(1);
      printf("CRASH COMMIT COMPLETE\n");
      exit(0);
    }
    exit(1);
  }
  memset(buffer, 'b', sizeof(buffer));
  printf("BENCH columns: kib repetition outcome begin_ticks write_ticks final_ticks\n");
  for (int kib = 1; kib <= 64; kib *= 2) {
    for (int repetition = 0; repetition < 3; repetition++) {
      uint t0 = uptime();
      if (xbegin()) exit(1);
      uint t1 = uptime();
      int fd = open("spbench", O_CREATE | O_TRUNC | O_WRONLY), failed = fd < 0;
      for (int i = 0; !failed && i < kib; i++)
        if (write(fd, buffer, sizeof(buffer)) != sizeof(buffer)) failed = 1;
      if (fd >= 0) close(fd);
      uint t2 = uptime();
      int result = xcommit();
      uint t3 = uptime();
      printf("BENCH %d %d %s %d %d %d\n", kib, repetition,
             failed || result ? "ABORT" : "COMMIT", t1-t0, t2-t1, t3-t2);
      if (!result) unlink("spbench");
    }
  }
  printf("BENCH COMPLETE\n");
  exit(0);
}
