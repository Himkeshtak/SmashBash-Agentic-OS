#include "kernel/types.h"
#include "kernel/stat.h"
#include "user/user.h"
#include "kernel/fcntl.h"

int
main(int argc, char *argv[])
{
  printf("\n=== RUNNING CONFLICT RESOLUTION TEST (conflicttest) ===\n");
  printf("Policy: First-Committer-Wins (Abort on Conflict)\n\n");

  unlink("cfile");
  int fd = open("cfile", O_CREATE | O_WRONLY);
  if (fd < 0) {
    printf("FAILED: cannot create cfile\n");
    exit(1);
  }
  write(fd, "initial_data", 12);
  close(fd);

  int pid = fork();
  if (pid < 0) {
    printf("FAILED: fork failed\n");
    exit(1);
  }

  if (pid == 0) {
    // Child process enters speculation
    if (xbegin() < 0) {
      printf("CHILD FAILED: xbegin failed\n");
      exit(1);
    }

    // Speculatively modify cfile
    fd = open("cfile", O_WRONLY);
    if (fd < 0) {
      printf("CHILD FAILED: open cfile\n");
      exit(1);
    }
    write(fd, "child_shadow", 12);
    close(fd);

    // Sleep 10 ticks to let parent perform outside write to cfile
    pause(10);

    // Child attempts to commit: MUST FAIL with -1 due to conflict!
    int res = xcommit();
    if (res == 0) {
      printf("CHILD FAILED: xcommit succeeded despite outside conflicting write!\n");
      exit(1);
    } else {
      printf("CHILD: xcommit correctly rejected with %d (Conflict Invalidation)\n", res);
    }

    exit(0);
  }

  // Parent process (spec == 0)
  // Wait 2 ticks so child has shadowed cfile
  pause(2);

  // Outside write to cfile
  fd = open("cfile", O_WRONLY);
  if (fd < 0) {
    printf("PARENT FAILED: open cfile\n");
    exit(1);
  }
  write(fd, "outside_wins", 12);
  close(fd);

  wait(0);

  // Verify real disk content: outside_wins must be intact
  fd = open("cfile", O_RDONLY);
  if (fd < 0) {
    printf("FAILED: cfile missing\n");
    exit(1);
  }
  char buf[32];
  memset(buf, 0, sizeof(buf));
  read(fd, buf, 12);
  close(fd);

  if (strcmp(buf, "outside_wins") != 0) {
    printf("FAILED: outside write overwritten: %s\n", buf);
    exit(1);
  }

  unlink("cfile");
  printf("\nPASSED: First-Committer-Wins conflict resolution successfully verified!\n");
  exit(0);
}
