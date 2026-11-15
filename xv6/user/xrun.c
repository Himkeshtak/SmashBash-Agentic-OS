// Run any ordinary xv6 program in a kernel speculation. The controller joins
// the child before asking the kernel to publish; a failed command is discarded.
#include "kernel/types.h"
#include "user/user.h"

int
main(int argc, char **argv)
{
  int preview = argc > 1 && strcmp(argv[1], "-n") == 0;
  int first = preview ? 2 : 1;
  if (argc <= first) {
    fprintf(2, "usage: xrun [-n] program [args ...]\n");
    exit(1);
  }
  if (xbegin() < 0) {
    fprintf(2, "xrun: cannot begin\n");
    exit(1);
  }
  int child = fork(), status = 1;
  if (child == 0) {
    exec(argv[first], argv + first);
    exit(1);
  }
  if (child < 0 || wait(&status) < 0 || status != 0 || preview) {
    xabort();
    if (status || child < 0) fprintf(2, "xrun: aborted\n");
    exit(status || child < 0 ? 1 : 0);
  }
  if (xcommit() < 0) {
    xabort();
    fprintf(2, "xrun: commit rejected\n");
    exit(1);
  }
  exit(0);
}
