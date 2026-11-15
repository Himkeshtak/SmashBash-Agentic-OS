#include "types.h"
#include "param.h"
#include "riscv.h"
#include "spinlock.h"
#include "sleeplock.h"
#include "fs.h"
#include "file.h"
#include "buf.h"
#include "proc.h"
#include "defs.h"
#include "syscall.h"
#include "spec.h"
#include "fcntl.h"

#define VIEWPAGES ((FSSIZE * BSIZE + PGSIZE - 1) / PGSIZE)
struct speculation {
  struct spinlock lock;
  int id, owner, members, bad, state, ndirty, budget;
  char *pages[VIEWPAGES];
  uchar valid[FSSIZE], dirty[FSSIZE];
  int nout, nbytes, npipes;
  struct { struct pipe *pipe; int offset, length; } out[SPEC_RECORDS];
  char output[SPEC_OUTPUT];
  struct pipe *pipes[SPEC_PIPES];
};
static struct speculation groups[NSPEC];
static struct sleeplock fsgate;
static uint nextid = 1;
extern struct superblock sb;

static struct speculation *
lookup(int id)
{
  for (int i = 0; i < NSPEC; i++)
    if (id > 0 && groups[i].id == id)
      return &groups[i];
  return 0;
}

void
specinit(void)
{
  initsleeplock(&fsgate, "spec filesystem gate");
  for (int i = 0; i < NSPEC; i++)
    initlock(&groups[i].lock, "speculation");
}

int
specid(void)
{
  struct proc *p = myproc();
  return p ? (p->specio >= 0 ? p->specio : p->spec) : 0;
}

void
spec_fsenter(void)
{
  struct proc *p = myproc();
  if (p->fsgate)
    return;
  acquiresleep(&fsgate);
  p->fsgate = 1;
}

void
spec_fsexit(void)
{
  struct proc *p = myproc();
  if (p->fsgate) {
    p->fsgate = 0;
    releasesleep(&fsgate);
  }
}

int
spec_failed(void)
{
  struct speculation *g = lookup(myproc()->spec);
  if (!g)
    return 0;
  acquire(&g->lock);
  int bad = g->bad;
  release(&g->lock);
  return bad;
}

void
spec_fail(void)
{
  struct speculation *g = lookup(myproc()->spec);
  if (g) {
    acquire(&g->lock);
    int newly_failed = !g->bad;
    g->bad = 1;
    release(&g->lock);
    if (newly_failed) spec_wakemembers(g->id);
  }
}

// The filesystem gate serializes buffer/inode operations and final publication.
// Blocking pipe/console reads and wait() deliberately do not hold this gate.
static int
filesystem_call(int num)
{
  struct proc *p = myproc();
  if (num == SYS_read || num == SYS_write || num == SYS_close || num == SYS_fstat) {
    int fd = p->trapframe->a0;
    struct file *f = fd >= 0 && fd < NOFILE ? p->ofile[fd] : 0;
    return f && (f->type == FD_INODE || (num != SYS_read && num != SYS_write && f->type == FD_DEVICE));
  }
  return num == SYS_open || num == SYS_unlink || num == SYS_link || num == SYS_mkdir ||
         num == SYS_mknod || num == SYS_chdir || num == SYS_exec || num == SYS_fork ||
         num == SYS_dup || num == SYS_sync || num == SYS_shadowcheck ||
         num == SYS_xbegin || num == SYS_xcommit || num == SYS_xabort || num == SYS_xstate;
}

int
spec_dirtycount(int id)
{
  struct speculation *g = lookup(id);
  return g ? g->ndirty : -1;
}

int
spec_sysenter(int num)
{
  if (filesystem_call(num))
    spec_fsenter();
  if (myproc()->spec && num == SYS_mknod) {
    spec_fail();
    return -1;
  }
  if (myproc()->spec && num == SYS_kill) {
    // Process termination is an irreversible outside effect. Limit it to
    // members of the caller's group rather than pretending it can roll back.
    if (!spec_samegroup(myproc()->trapframe->a0, myproc()->spec)) {
      spec_fail();
      return -1;
    }
  }
  if (spec_failed() && num != SYS_exit && num != SYS_close && num != SYS_wait &&
      num != SYS_kill && num != SYS_pause && num != SYS_getpid && num != SYS_uptime &&
      num != SYS_xabort && num != SYS_xcommit)
    return -1;
  return 0;
}

int
spec_index(int id)
{
  for (int i = 0; i < NSPEC; i++)
    if (id && groups[i].id == id)
      return i + 1;
  return 0;
}

int
spec_track_pipe(struct pipe *pi)
{
  struct speculation *g = lookup(myproc()->spec);
  if (!g)
    return 0;
  pipehold(pi);
  acquire(&g->lock);
  for (int i = 0; i < g->npipes; i++) {
    if (g->pipes[i] == pi) {
      release(&g->lock);
      pipeunhold(pi);
      return 0;
    }
  }
  if (g->bad || g->npipes == SPEC_PIPES) {
    g->bad = 1;
    release(&g->lock);
    pipeunhold(pi);
    return -1;
  }
  g->pipes[g->npipes++] = pi;
  release(&g->lock);
  return 0;
}

int
spec_output(struct pipe *pi, int user_src, uint64 src, int length)
{
  struct speculation *g = lookup(myproc()->spec);
  if (!g || length < 0)
    return -1;
  if (!length)
    return 0;
  if (pi && spec_track_pipe(pi) < 0)
    return -1;
  // Register before publishing the record: abort must never decrement a
  // pending record that its writer has not yet registered with the pipe.
  if (pi) pipepending(pi, 1);
  acquire(&g->lock);
  if (g->bad || g->nout == SPEC_RECORDS || length > SPEC_OUTPUT - g->nbytes) {
    g->bad = 1;
    release(&g->lock);
    if (pi) pipepending(pi, -1);
    return -1;
  }
  int record = g->nout++, offset = g->nbytes;
  g->nbytes += length;
  g->out[record].pipe = pi;
  g->out[record].offset = offset;
  g->out[record].length = length;
  release(&g->lock);
  if (either_copyin(g->output + offset, user_src, src, length) < 0) {
    spec_fail();
    return -1;
  }
  return length;
}

static void
finishpipes(struct speculation *g, int commit)
{
  for (int i = 0; i < g->npipes; i++)
    pipespecfinish(g->pipes[i], g->id, commit);
}

static void
releasepipes(struct speculation *g)
{
  for (int i = 0; i < g->npipes; i++)
    pipeunhold(g->pipes[i]);
  g->npipes = 0;
}

void
spec_sysleave(int num)
{
  struct proc *p = myproc();
  if (p->spec && p->trapframe->a0 == (uint64)-1 &&
      (num == SYS_write || num == SYS_unlink || num == SYS_link ||
       num == SYS_mkdir || num == SYS_mknod ||
       (num == SYS_open && (p->trapframe->a1 & (O_CREATE | O_TRUNC)))))
    spec_fail();
  if (p->spec && spec_failed() && num != SYS_close && num != SYS_wait &&
      num != SYS_kill && num != SYS_pause && num != SYS_getpid && num != SYS_uptime && num != SYS_xabort)
    p->trapframe->a0 = -1;
  spec_fsexit();
}

static char *
blockdata(struct speculation *g, uint block)
{
  return g->pages[block * BSIZE / PGSIZE] + (block * BSIZE % PGSIZE);
}

void
spec_load(struct buf *b)
{
  struct speculation *g = lookup(b->spec);
  if (!g || b->dev != ROOTDEV || b->blockno >= sb.size)
    panic("invalid speculative buffer");
  uint block = b->blockno;
  if (!g->valid[block]) {
    struct buf *real = bread_committed(b->dev, block);
    memmove(blockdata(g, block), real->data, BSIZE);
    brelse(real);
    g->valid[block] = 1;
  }
  memmove(b->data, blockdata(g, block), BSIZE);
  b->valid = 1;
}

void
spec_store(struct buf *b)
{
  struct speculation *g = lookup(b->spec);
  if (!g)
    panic("orphan speculative write");
  memmove(blockdata(g, b->blockno), b->data, BSIZE);
  if (!g->dirty[b->blockno]) {
    g->dirty[b->blockno] = 1;
    if (++g->ndirty > g->budget)
      spec_fail();
  }
}

void
spec_modified(uint block)
{
  if (block >= FSSIZE)
    return;
  for (int i = 0; i < NSPEC; i++) {
    struct speculation *g = &groups[i];
    acquire(&g->lock);
    int invalidated = g->id && g->state == SPEC_ACTIVE && g->valid[block] && !g->bad;
    if (invalidated)
      g->bad = 1; // Outside writer wins; private read/write views cannot publish.
    release(&g->lock);
    if (invalidated) spec_wakemembers(g->id);
  }
}

static void
freegroup(struct speculation *g)
{
  for (int i = 0; i < VIEWPAGES; i++) {
    if (g->pages[i])
      kfree(g->pages[i]);
    g->pages[i] = 0;
  }
  binvalidate(g->id);
  acquire(&g->lock);
  g->id = 0;
  g->state = SPEC_FREE;
  release(&g->lock);
}

static void
dropmember(struct speculation *g)
{
  acquire(&g->lock);
  int last = --g->members == 0;
  release(&g->lock);
  if (last)
    freegroup(g);
}

int
spec_begin(void)
{
  struct proc *p = myproc();
  if (p->spec || sb.size > FSSIZE || sb.ninodes > SPEC_INODES || nextid > 0x7fffffff)
    return -1;
  struct speculation *g = 0;
  for (int i = 0; i < NSPEC; i++)
    if (groups[i].state == SPEC_FREE) {
      g = &groups[i];
      break;
    }
  if (!g)
    return -1;
  memset(g->valid, 0, sizeof(g->valid));
  memset(g->dirty, 0, sizeof(g->dirty));
  g->ndirty = g->bad = 0;
  g->nout = g->nbytes = g->npipes = 0;
  g->budget = sb.nlog - 1 < SPEC_DIRTY ? sb.nlog - 1 : SPEC_DIRTY;
  g->owner = p->pid;
  g->members = 1;
  g->id = nextid++;
  g->state = SPEC_ACTIVE;
  // Reserve backing RAM before admitting any effects; copies remain lazy.
  for (int i = 0; i < (sb.size * BSIZE + PGSIZE - 1) / PGSIZE; i++) {
    if (!(g->pages[i] = kalloc())) {
      freegroup(g);
      return -1;
    }
  }
  p->spec = g->id;
  p->savedcwd = p->cwd;
  p->cwd = igetview(p->cwd->dev, p->cwd->inum, g->id);
  for (int i = 0; i < NOFILE; i++) {
    struct file *old = p->ofile[i];
    if (!old)
      continue;
    struct file *copy = 0;
    for (int j = 0; j < i; j++)
      if (p->savedfile[j] == old) {
        copy = filedup(p->ofile[j]);
        break;
      }
    if (!copy && old->type == FD_PIPE)
      copy = filedup(old);
    if (!copy) {
      copy = filealloc();
      if (!copy) {
        spec_abort();
        return -1;
      }
      *copy = *old;
      copy->ref = 1;
      copy->ip = igetview(old->ip->dev, old->ip->inum, g->id);
    }
    p->savedfile[i] = old;
    p->ofile[i] = copy;
    pipebackup(copy, g->id, 1);
    pipefdchange(copy, g->id, 1);
    pipefdchange(copy, 0, -1);
  }
  p->spec_ready = 1;
  return 0;
}

static void
restorefiles(int commit)
{
  struct proc *p = myproc();
  if (commit) {
    p->specio = 0;
    begin_op();
    for (int i = 0; i < NOFILE; i++) {
      struct file *f = p->ofile[i];
      pipefdchange(f, 0, 1);
      pipefdchange(f, p->spec, -1);
      if (f && (f->type == FD_INODE || f->type == FD_DEVICE) && f->ip->spec) {
        struct inode *old = f->ip;
        f->ip = igetview(old->dev, old->inum, 0);
        ilock(f->ip);
        iunlock(f->ip);
        iput(old);
      }
    }
    struct inode *old = p->cwd;
    p->cwd = igetview(old->dev, old->inum, 0);
    ilock(p->cwd);
    iunlock(p->cwd);
    iput(old);
    for (int i = 0; i < NOFILE; i++) {
      pipebackup(p->savedfile[i], p->spec, -1);
      if (p->savedfile[i])
        fileclose(p->savedfile[i]);
      p->savedfile[i] = 0;
    }
    iput(p->savedcwd);
    end_op();
  } else {
    // Clone failure can leave some descriptors unconverted; only replace saved entries.
    for (int i = 0; i < NOFILE; i++) {
      if (p->spec_ready || p->savedfile[i]) {
        if (p->ofile[i]) {
          pipefdchange(p->ofile[i], p->spec, -1);
          fileclose(p->ofile[i]);
        }
        p->ofile[i] = p->savedfile[i];
        pipefdchange(p->ofile[i], 0, 1);
        pipebackup(p->ofile[i], p->spec, -1);
        p->savedfile[i] = 0;
      } else if (p->ofile[i] && p->ofile[i]->type != FD_PIPE && p->ofile[i]->ip->spec == p->spec) {
        fileclose(p->ofile[i]);
        p->ofile[i] = 0;
      }
    }
    iput(p->cwd);
    p->cwd = p->savedcwd;
  }
  p->savedcwd = 0;
  p->spec_ready = 0;
  p->specio = -1;
}

int
spec_abort(void)
{
  struct proc *p = myproc();
  struct speculation *g = lookup(p->spec);
  if (!g || g->owner != p->pid)
    return -1;
  spec_fail();
  g->state = SPEC_CANCELLED;
  spec_killmembers(g->id, p->pid);
  for (int i = 0; i < g->nout; i++)
    if (g->out[i].pipe) pipepending(g->out[i].pipe, -1);
  finishpipes(g, 0);
  releasepipes(g);
  restorefiles(0);
  p->spec = 0;
  dropmember(g);
  return 0;
}

int
spec_commit(void)
{
  struct proc *p = myproc();
  struct speculation *g = lookup(p->spec);
  if (!g || g->owner != p->pid)
    return -1;
  if (spec_failed()) {
    spec_abort();
    return -1;
  }
  if (g->members != 1)
    return -1; // Controller must join children before publication.
  if (shadow_validate() < 0 || g->ndirty > g->budget) {
    spec_abort();
    return -1;
  }
  g->state = SPEC_COMMITTING;
  p->specio = 0;
  if (log_begin_spec(g->ndirty) < 0) {
    p->specio = -1;
    spec_abort();
    return -1;
  }
  for (uint block = 0; block < sb.size; block++) {
    if (!g->dirty[block])
      continue;
    struct buf *real = bread_committed(ROOTDEV, block);
    memmove(real->data, blockdata(g, block), BSIZE);
    log_write(real);
    brelse(real);
  }
  end_op(); // Exclusive gate + sole log operation: synchronous durable commit.
  finishpipes(g, 1);
  iinvalidate();
  restorefiles(1);
  p->spec = 0;
  // Never block on external output while holding the filesystem gate.
  spec_fsexit();
  for (int i = 0; i < g->nout; i++) {
    char *data = g->output + g->out[i].offset;
    int length = g->out[i].length;
    if (g->out[i].pipe) {
      pipewrite_kernel(g->out[i].pipe, data, length);
      pipepending(g->out[i].pipe, -1);
    } else
      uartwrite(data, length);
  }
  releasepipes(g);
  spec_fsenter();
  dropmember(g);
  return 0;
}

void
spec_fork(struct proc *child)
{
  struct proc *p = myproc();
  child->spec = p->spec;
  if (p->spec) {
    struct speculation *g = lookup(p->spec);
    acquire(&g->lock);
    g->members++;
    release(&g->lock);
  }
}

void
spec_exit(void)
{
  struct proc *p = myproc();
  struct speculation *g = lookup(p->spec);
  if (!g)
    return;
  if (g->owner == p->pid) {
    spec_abort();
    return;
  }
  // Called after private descriptors/cwd have been closed by kexit().
  p->spec = 0;
  dropmember(g);
}
