// SB1: bounded agent-independent filesystem transaction service.
// No command strings, executable payloads, network operations, or raw devices.
#include "kernel/types.h"
#include "kernel/stat.h"
#include "kernel/fs.h"
#include "kernel/fcntl.h"
#include "user/user.h"

#define MAXOPS 32
#define MAXENTRIES 128
#define MAXBYTES 262144
#define MAXFILEBYTES 65536
#define PATHLIMIT 96
#define MAXDEPTH 8

static char line[512], root[26], path[128], destination[128];
static uchar payload[MAXFILEBYTES];
static int planned, completed, maxentries, maxbytes, maxfile;
static int active, pending, expected, received, prepared;
static char opcode;
static uint seen[MAXENTRIES];
static int entries, bytes;

static void
abort_tx(char *reason)
{
  printf("@SB1 ABORT %s\n", reason);
  exit(1);
}

static void
ok(void)
{
  printf("@SB1 OK\n");
}

static int
number(char *s)
{
  int n = 0;
  if (!*s)
    abort_tx("number");
  for (; *s; s++) {
    if (*s < '0' || *s > '9' || n > MAXBYTES)
      abort_tx("number");
    n = n * 10 + *s - '0';
  }
  if (n > MAXBYTES)
    abort_tx("number");
  return n;
}

static int
nibble(char c)
{
  if (c >= '0' && c <= '9')
    return c - '0';
  if (c >= 'a' && c <= 'f')
    return c - 'a' + 10;
  abort_tx("hex");
  return -1;
}

static int
unhex(char *s, uchar *out, int capacity)
{
  int n = strlen(s);
  if (n % 2 || n / 2 > capacity)
    abort_tx("hex-length");
  for (int i = 0; i < n; i += 2)
    out[i / 2] = (nibble(s[i]) << 4) | nibble(s[i + 1]);
  return n / 2;
}

static int
split(char **fields)
{
  int n = 0;
  char *p = line;
  while (*p) {
    if (n == 8 || *p == ' ')
      abort_tx("fields");
    fields[n++] = p;
    while (*p && *p != ' ')
      p++;
    if (*p)
      *p++ = 0;
  }
  return n;
}

static void
readline(void)
{
  int length = 0;
  char c;
  for (;;) {
    if (read(0, &c, 1) != 1)
      exit(1);
    if (c == '\n')
      break;
    if (c < 32 || c > 126 || length >= sizeof(line) - 1)
      abort_tx("frame");
    line[length++] = c;
  }
  line[length] = 0;
}

static int
canonical(char *s, int allow_dot)
{
  if (allow_dot && strcmp(s, ".") == 0)
    return 1;
  int component = 0, depth = 1;
  char *start = s;
  if (!*s || strlen(s) > PATHLIMIT)
    return 0;
  for (;;) {
    char c = *s;
    if (c == '/' || c == 0) {
      if (component == 0 || component > DIRSIZ ||
          (component == 1 && start[0] == '.') ||
          (component == 2 && start[0] == '.' && start[1] == '.'))
        return 0;
      if (!c)
        return 1;
      if (++depth > MAXDEPTH)
        return 0;
      component = 0;
      start = s + 1;
    } else {
      if (!((c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') ||
            (c >= '0' && c <= '9') || c == '_' || c == '-' || c == '.'))
        return 0;
      component++;
    }
    s++;
  }
}

static void
decode_path(char *encoded, char *out, int allow_dot)
{
  char relative[PATHLIMIT + 1];
  int n = unhex(encoded, (uchar *)relative, PATHLIMIT);
  relative[n] = 0;
  if (strlen(relative) != n || !canonical(relative, allow_dot))
    abort_tx("path");
  strcpy(out, root);
  if (strcmp(relative, ".") != 0) {
    strcpy(out + strlen(out), "/");
    strcpy(out + strlen(out), relative);
  }
}

static void
regular(char *p, int may_create)
{
  struct stat st;
  if (stat(p, &st) < 0) {
    if (may_create)
      return;
    abort_tx("missing-file");
  }
  if (st.type != T_FILE || st.nlink != 1)
    abort_tx("file-type-or-links");
}

static void
execute(void)
{
  struct stat st;
  int fd, n;
  char buffer[256];
  if (received != expected)
    abort_tx("incomplete-payload");
  switch (opcode) {
  case 'W':
  case 'A':
    regular(path, 1);
    if (stat(path, &st) == 0 && opcode == 'A' && st.size + received > maxfile)
      abort_tx("file-quota");
    fd = open(path, O_RDWR | O_CREATE | (opcode == 'W' ? O_TRUNC : 0));
    if (fd < 0)
      abort_tx("open-write");
    if (opcode == 'A') {
      while ((n = read(fd, buffer, sizeof(buffer))) > 0)
        ;
      if (n < 0)
        abort_tx("append-read");
    }
    if (write(fd, payload, received) != received || close(fd) < 0)
      abort_tx("short-write");
    break;
  case 'M':
    if (mkdir(path) < 0)
      abort_tx("mkdir");
    break;
  case 'U':
    if (stat(path, &st) < 0 || (st.type != T_FILE && st.type != T_DIR) ||
        (st.type == T_FILE && st.nlink != 1) || unlink(path) < 0)
      abort_tx("remove");
    break;
  case 'N':
    regular(path, 0);
    if (stat(destination, &st) == 0 || link(path, destination) < 0 || unlink(path) < 0)
      abort_tx("rename");
    break;
  case 'R':
    regular(path, 0);
    fd = open(path, O_RDONLY);
    if (fd < 0)
      abort_tx("open-read");
    while ((n = read(fd, buffer, sizeof(buffer))) > 0)
      ;
    if (n < 0 || close(fd) < 0)
      abort_tx("read");
    break;
  case 'L':
  case 'S':
    if (stat(path, &st) < 0 || (st.type != T_DIR && st.type != T_FILE) ||
        (opcode == 'L' && st.type != T_DIR))
      abort_tx("stat-or-list");
    break;
  default:
    abort_tx("opcode");
  }
  completed++;
  pending = 0;
}

static void
inspect(char *p, int depth)
{
  struct stat st;
  struct dirent de;
  char child[128], name[DIRSIZ + 1];
  int fd, n, subdirs = 0;
  if (depth > MAXDEPTH || stat(p, &st) < 0 || entries >= maxentries)
    abort_tx("tree-limit");
  if (st.type != T_DIR && st.type != T_FILE)
    abort_tx("special-file");
  for (int i = 0; i < entries; i++)
    if (seen[i] == st.ino)
      abort_tx("inode-alias");
  seen[entries++] = st.ino;
  if (st.type == T_FILE) {
    if (st.nlink != 1 || st.size > maxfile || st.size > maxbytes - bytes)
      abort_tx("file-limit");
    bytes += st.size;
    return;
  }
  if (st.size % sizeof(de))
    abort_tx("directory-size");
  fd = open(p, O_RDONLY);
  if (fd < 0)
    abort_tx("directory-open");
  while ((n = read(fd, &de, sizeof(de))) == sizeof(de)) {
    if (!de.inum)
      continue;
    memmove(name, de.name, DIRSIZ);
    name[DIRSIZ] = 0;
    if (strcmp(name, ".") == 0 || strcmp(name, "..") == 0)
      continue;
    if (!canonical(name, 0) || strlen(p) + 1 + strlen(name) >= sizeof(child))
      abort_tx("directory-name");
    strcpy(child, p);
    strcpy(child + strlen(child), "/");
    strcpy(child + strlen(child), name);
    struct stat cs;
    if (stat(child, &cs) < 0)
      abort_tx("directory-entry");
    if (cs.type == T_DIR)
      subdirs++;
    inspect(child, depth + 1);
  }
  if (n != 0 || close(fd) < 0 || st.nlink != 1 + subdirs)
    abort_tx("directory-links");
}

int
main(void)
{
  printf("@SB1 READY 1\n");
  for (;;) {
    char *fields[8];
    readline();
    int count = split(fields);
    if (!count || strlen(fields[0]) != 1 || prepared)
      abort_tx("state");
    char command = fields[0][0];
    if (command == 'B' && count == 6 && !active) {
      planned = number(fields[1]);
      maxentries = number(fields[2]);
      maxbytes = number(fields[3]);
      maxfile = number(fields[4]);
      int n = unhex(fields[5], (uchar *)root, sizeof(root) - 1);
      root[n] = 0;
      if (strlen(root) != n || root[0] != '/' || !canonical(root + 1, 0) ||
          strchr(root + 1, '/') || strcmp(root, "/console") == 0 ||
          planned > MAXOPS || maxentries < 1 || maxentries > MAXENTRIES ||
          maxbytes < 1 || maxbytes > MAXBYTES || maxfile < 1 || maxfile > MAXFILEBYTES)
        abort_tx("policy-limits");
      struct stat st;
      if (stat(root, &st) < 0) {
        if (mkdir(root) < 0)
          abort_tx("root-create");
      } else if (st.type != T_DIR) {
        abort_tx("root-type");
      }
      active = 1;
      ok();
    } else if (command == 'O' && count == 5 && active && !pending && completed < planned) {
      if (strlen(fields[1]) != 1 || !strchr("WAMUNRLS", fields[1][0]))
        abort_tx("opcode");
      opcode = fields[1][0];
      decode_path(fields[2], path, opcode == 'L' || opcode == 'S');
      if (opcode == 'N')
        decode_path(fields[3], destination, 0);
      else if (strcmp(fields[3], "-") != 0)
        abort_tx("destination");
      expected = number(fields[4]);
      if (expected > maxfile || ((opcode != 'W' && opcode != 'A') && expected))
        abort_tx("payload-limit");
      received = 0;
      pending = 1;
      ok();
    } else if (command == 'D' && count == 2 && pending) {
      int n = unhex(fields[1], payload + received, expected - received);
      if (!n)
        abort_tx("empty-chunk");
      received += n;
      ok();
    } else if (command == 'E' && count == 1 && pending) {
      execute();
      // Validate the whole managed tree after each operation, and again at F.
      entries = bytes = 0;
      inspect(root, 0);
      ok();
    } else if (command == 'F' && count == 1 && active && !pending && completed == planned) {
      entries = bytes = 0;
      inspect(root, 0);
      if (sync() < 0 || shadowcheck() < 0)
        abort_tx("kernel-integrity");
      prepared = 1;
      printf("@SB1 PREPARED %d %d %d\n", completed, entries, bytes);
      // Do not return to the shell, which could accept more mutable commands.
    } else {
      abort_tx("state-or-frame");
    }
  }
}
