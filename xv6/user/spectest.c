#include "kernel/types.h"
#include "kernel/stat.h"
#include "kernel/fcntl.h"
#include "user/user.h"

static void
check(int condition, char *message)
{
  if (!condition) {
    xabort();
    fprintf(2, "SPECTEST FAIL: %s\n", message);
    exit(1);
  }
}

static void
put(char *path, char *data)
{
  int fd = open(path, O_CREATE | O_WRONLY | O_TRUNC);
  check(fd >= 0, "open write");
  check(write(fd, data, strlen(data)) == strlen(data), "write");
  close(fd);
}

static int
equals(char *path, char *text)
{
  char data[128];
  int fd = open(path, O_RDONLY);
  if (fd < 0)
    return 0;
  int n = read(fd, data, sizeof(data));
  close(fd);
  return n == strlen(text) && memcmp(data, text, n) == 0;
}

static void
lifecycle(void)
{
  check(xcommit() == -1 && xabort() == -1, "invalid finalization");
  check(xbegin() == 0, "begin");
  check(xbegin() == -1, "nested begin");
  check(xabort() == 0, "abort");
  for (int i = 0; i < 12; i++) {
    check(xbegin() == 0, "repeat begin");
    check(xabort() == 0, "repeat abort");
  }
  printf("SPECTEST lifecycle PASS\n");
}

static void
isolation(void)
{
  put("spbase", "old");
  int release[2];
  check(pipe(release) == 0, "sync pipe");
  int child = fork();
  check(child >= 0, "fork");
  if (child == 0) {
    char c;
    close(release[1]);
    check(xbegin() == 0, "child begin");
    put("spbase", "private-value");
    check(equals("spbase", "private-value"), "read own write");
    read(release[0], &c, 1);
    check(xabort() == 0, "child abort");
    exit(0);
  }
  close(release[0]);
  while (xstate(child) < 1)
    pause(1);
  check(equals("spbase", "old"), "outside sees committed inode and data");
  write(release[1], "r", 1);
  int status;
  wait(&status);
  check(status == 0 && equals("spbase", "old"), "abort preserves committed bytes");
  close(release[1]);
  unlink("spbase");
  printf("SPECTEST isolation PASS\n");
}

static void
namespace(void)
{
  put("spold", "keep");
  check(xbegin() == 0, "namespace begin");
  check(unlink("spold") == 0, "private unlink");
  check(mkdir("spdir") == 0, "private mkdir");
  put("spdir/new", "created");
  check(equals("spdir/new", "created"), "private new inode");
  check(xabort() == 0, "namespace abort");
  check(equals("spold", "keep") && open("spdir", O_RDONLY) == -1, "namespace rollback");
  check(xbegin() == 0, "commit begin");
  check(unlink("spold") == 0 && mkdir("spdir") == 0, "commit namespace");
  put("spdir/new", "committed");
  check(xcommit() == 0, "logged commit");
  check(equals("spdir/new", "committed") && open("spold", O_RDONLY) == -1, "published namespace");
  unlink("spdir/new"); unlink("spdir");
  check(shadowcheck() == 0, "post-commit integrity");
  printf("SPECTEST namespace PASS\n");
}

static void
inheritance(void)
{
  check(xbegin() == 0, "inherit begin");
  put("spshare", "parent");
  int child = fork();
  check(child >= 0, "inherit fork");
  if (child == 0) {
    check(equals("spshare", "parent"), "inherited view");
    check(xcommit() == -1, "child cannot commit");
    put("spshare", "child");
    exit(0);
  }
  int status;
  wait(&status);
  check(status == 0 && equals("spshare", "child"), "group shares child writes");
  check(xcommit() == 0 && equals("spshare", "child"), "group commit");
  unlink("spshare");
  printf("SPECTEST inheritance PASS\n");
}

static void
outputs(void)
{
  check(xbegin() == 0, "console abort begin");
  printf("CONSOLE_MUST_NOT_ESCAPE\n");
  check(xabort() == 0, "console abort");
  check(xbegin() == 0, "console commit begin");
  printf("CONSOLE_COMMITTED_ONCE\n");
  check(xcommit() == 0, "console commit");

  // A pipe wholly inside the group must work before commit, including EOF.
  check(xbegin() == 0, "internal begin");
  int p[2], status;
  check(pipe(p) == 0, "internal pipe");
  if (fork() == 0) {
    char c;
    close(p[1]);
    check(read(p[0], &c, 1) == 1 && c == 'i', "internal traffic");
    check(read(p[0], &c, 1) == 0, "internal EOF");
    close(p[0]);
    exit(0);
  }
  close(p[0]);
  check(write(p[1], "i", 1) == 1, "internal write");
  close(p[1]);
  wait(&status);
  check(status == 0 && xcommit() == 0, "internal join and commit");

  // An inherited pipe's unconsumed private bytes disappear on abort.
  check(pipe(p) == 0, "purge pipe");
  check(write(p[1], "b", 1) == 1 && xbegin() == 0, "baseline pipe");
  check(write(p[1], "x", 1) == 1 && xabort() == 0, "purge abort");
  close(p[1]);
  char b[8];
  check(read(p[0], b, sizeof(b)) == 1 && b[0] == 'b', "purge preserves baseline");
  check(read(p[0], b, 1) == 0, "purge EOF");
  close(p[0]);

  // Only an outside reader remains. Closing the trial's writer must not
  // deliver premature EOF or free the deferred-output destination.
  for (int commit = 0; commit < 2; commit++) {
    check(pipe(p) == 0, "external pipe");
    int child = fork();
    check(child >= 0, "external fork");
    if (child == 0) {
      close(p[0]);
      check(xbegin() == 0, "external begin");
      check(write(p[1], "external", 8) == 8, "deferred write");
      close(p[1]);
      pause(2);
      check((commit ? xcommit() : xabort()) == 0, "external finalize");
      exit(0);
    }
    close(p[1]);
    int n = read(p[0], b, sizeof(b));
    check(n == (commit ? 8 : 0), "external visibility");
    if (commit) check(memcmp(b, "external", 8) == 0, "external bytes");
    check(read(p[0], b, 1) == 0, "external EOF");
    close(p[0]);
    wait(&status);
    check(status == 0, "external child");
  }

  // A mixed audience cannot safely receive private bytes eagerly.
  check(pipe(p) == 0, "mixed pipe");
  int release[2];
  check(pipe(release) == 0, "mixed release");
  int child = fork();
  if (child == 0) {
    close(p[1]); close(release[1]);
    read(release[0], b, 1);
    exit(0);
  }
  close(release[0]);
  check(xbegin() == 0, "mixed begin");
  check(write(p[1], "bad", 3) == -1, "mixed rejected");
  check(xcommit() == -1, "mixed dooms transaction");
  write(release[1], "r", 1); close(release[1]);
  wait(&status); close(p[0]); close(p[1]);
  printf("SPECTEST outputs PASS\n");
}

static void
conflict(void)
{
  put("spconflict", "baseline");
  int release[2];
  check(pipe(release) == 0, "conflict pipe");
  int child = fork();
  check(child >= 0, "conflict fork");
  if (child == 0) {
    close(release[1]);
    check(xbegin() == 0, "conflict begin");
    put("spconflict", "speculative");
    char c;
    check(read(release[0], &c, 1) == -1, "conflict wakes blocked pipe read");
    check(xcommit() == -1, "outside writer invalidates group");
    exit(0);
  }
  close(release[0]);
  while (xstate(child) < 1) pause(1);
  pause(1);
  put("spconflict", "outside-wins");
  int status; wait(&status);
  close(release[1]);
  check(status == 0 && equals("spconflict", "outside-wins"), "preserve outside write");
  unlink("spconflict");
  printf("SPECTEST conflict PASS\n");
}

static char large[8193];
static void
members(void)
{
  int release[2], children[4], status;
  check(pipe(release) == 0, "capacity pipe");
  for (int i = 0; i < 4; i++) {
    children[i] = fork();
    check(children[i] >= 0, "capacity fork");
    if (children[i] == 0) {
      char c;
      close(release[1]);
      check(xbegin() == 0, "parallel begin");
      read(release[0], &c, 1);
      check(xabort() == 0, "parallel abort");
      exit(0);
    }
    while (xstate(children[i]) < 0) pause(1);
  }
  check(xbegin() == -1, "group table exhaustion");
  write(release[1], "rrrr", 4);
  close(release[0]); close(release[1]);
  for (int i = 0; i < 4; i++) {
    wait(&status);
    check(status == 0, "parallel cleanup");
  }
  check(xbegin() == 0, "busy begin");
  int p[2];
  check(pipe(p) == 0, "busy pipe");
  if (fork() == 0) {
    char c;
    close(p[1]); read(p[0], &c, 1); close(p[0]); exit(0);
  }
  close(p[0]);
  check(xcommit() == -1, "live child prevents commit");
  write(p[1], "r", 1); close(p[1]); wait(&status);
  check(status == 0 && xcommit() == 0, "joined child permits commit");

  // An exiting owner cancels descendants while they still carry their private
  // identity. They must never resume as ordinary committed writers.
  check(pipe(release) == 0, "death pipe");
  int owner = fork();
  if (owner == 0) {
    close(release[1]);
    check(xbegin() == 0, "death begin");
    put("spdead", "private");
    if (fork() == 0) {
      for (;;) { put("spescape", "private"); pause(1); }
    }
    char c; read(release[0], &c, 1);
    exit(0);
  }
  close(release[0]);
  while (xstate(owner) < 1) pause(1);
  write(release[1], "r", 1); close(release[1]); wait(&status);
  pause(2);
  check(status == 0 && open("spdead", O_RDONLY) == -1 &&
        open("spescape", O_RDONLY) == -1, "owner death rollback");
  check(xbegin() == 0 && xabort() == 0, "owner death reclaims group");
  printf("SPECTEST members PASS\n");
}

static void
limits(void)
{
  memset(large, 'z', sizeof(large));
  check(xbegin() == 0, "output limit begin");
  check(write(1, large, sizeof(large)) == -1, "bounded output");
  check(xcommit() == -1, "output overflow aborts");
  check(xbegin() == 0, "dirty limit begin");
  int fd = open("spbig", O_CREATE | O_WRONLY);
  check(fd >= 0, "limit open");
  int failed = 0;
  for (int i = 0; i < 10; i++)
    if (write(fd, large, 8192) < 0) { failed = 1; break; }
  close(fd);
  check(failed && xcommit() == -1, "dirty overflow aborts");
  check(open("spbig", O_RDONLY) == -1, "no dirty overflow leak");
  check(shadowcheck() == 0, "limit integrity");
  for (int i = 0; i < 8; i++)
    check(xbegin() == 0 && xabort() == 0, "exhaustion cleanup");
  printf("SPECTEST limits PASS\n");
}

static void
descriptors(void)
{
  put("spfd", "abcdef");
  int fd = open("spfd", O_RDONLY), alias = dup(fd);
  char b[8];
  check(xbegin() == 0, "fd begin");
  check(read(fd, b, 1) == 1 && b[0] == 'a', "private offset");
  check(read(alias, b, 1) == 1 && b[0] == 'b', "private dup alias");
  check(chdir("/") == 0 && xabort() == 0, "fd abort");
  check(read(fd, b, 1) == 1 && b[0] == 'a', "abort restores offset");
  close(fd); close(alias);
  // A committed unlinked inode stays usable through an outside open fd.
  fd = open("spfd", O_RDONLY);
  int child = fork();
  if (child == 0) {
    close(fd);
    check(xbegin() == 0 && unlink("spfd") == 0, "open unlink");
    check(xcommit() == 0, "open unlink commit");
    exit(0);
  }
  int status; wait(&status);
  check(status == 0, "unlink child");
  check(read(fd, b, sizeof(b)) == 6 && memcmp(b, "abcdef", 6) == 0, "outside open survives unlink");
  close(fd);
  check(shadowcheck() == 0, "unlink final cleanup");
  printf("SPECTEST descriptors PASS\n");
}

int
main(void)
{
  lifecycle();
  isolation();
  namespace();
  inheritance();
  outputs();
  conflict();
  limits();
  descriptors();
  members();
  printf("SPECTEST ALL PASS\n");
  exit(0);
}
