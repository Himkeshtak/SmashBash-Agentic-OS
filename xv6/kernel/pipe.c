#include "types.h"
#include "riscv.h"
#include "defs.h"
#include "param.h"
#include "spinlock.h"
#include "proc.h"
#include "fs.h"
#include "sleeplock.h"
#include "file.h"
#include "spec.h"

#define PIPESIZE 512
struct pipe {
  struct spinlock lock;
  char data[PIPESIZE];
  int tag[PIPESIZE];
  int readers[NSPEC + 1];
  int writers[NSPEC + 1], savedreaders[NSPEC + 1], savedwriters[NSPEC + 1];
  int pending;
  int holds;
  uint nread, nwrite;
  int readopen, writeopen;
};

int
pipealloc(struct file **f0, struct file **f1)
{
  struct pipe *pi = 0;
  *f0 = *f1 = 0;
  if ((*f0 = filealloc()) == 0 || (*f1 = filealloc()) == 0)
    goto bad;
  if ((pi = kalloc()) == 0)
    goto bad;
  memset(pi, 0, sizeof(*pi));
  pi->readopen = pi->writeopen = 1;
  initlock(&pi->lock, "pipe");
  (*f0)->type = FD_PIPE;
  (*f0)->readable = 1;
  (*f0)->writable = 0;
  (*f0)->pipe = pi;
  (*f1)->type = FD_PIPE;
  (*f1)->readable = 0;
  (*f1)->writable = 1;
  (*f1)->pipe = pi;
  return 0;
bad:
  if (pi)
    kfree(pi);
  if (*f0)
    fileclose(*f0);
  if (*f1)
    fileclose(*f1);
  return -1;
}

void
pipeclose(struct pipe *pi, int writable)
{
  acquire(&pi->lock);
  if (writable) {
    pi->writeopen = 0;
    wakeup(&pi->nread);
  } else {
    pi->readopen = 0;
    wakeup(&pi->nwrite);
  }
  int free = !pi->readopen && !pi->writeopen && !pi->holds;
  release(&pi->lock);
  if (free)
    kfree(pi);
}

static void
account(struct file *f, int id, int delta, int backup)
{
  if (!f || f->type != FD_PIPE)
    return;
  struct pipe *pi = f->pipe;
  int index = spec_index(id);
  acquire(&pi->lock);
  int *r = backup ? pi->savedreaders : pi->readers;
  int *w = backup ? pi->savedwriters : pi->writers;
  if (f->readable) r[index] += delta;
  if (f->writable) w[index] += delta;
  if (r[index] < 0 || w[index] < 0)
    panic("pipe endpoint accounting");
  if (delta < 0) {
    wakeup(&pi->nread);
    wakeup(&pi->nwrite);
  }
  release(&pi->lock);
}

void pipefdchange(struct file *f, int id, int delta) { account(f, id, delta, 0); }
void pipebackup(struct file *f, int id, int delta) { account(f, id, delta, 1); }

void
pipepending(struct pipe *pi, int delta)
{
  acquire(&pi->lock);
  pi->pending += delta;
  if (pi->pending < 0) panic("pipe pending accounting");
  wakeup(&pi->nread);
  release(&pi->lock);
}

// Saved endpoints keep the committed observer alive until finalization, but
// must not prevent EOF between members of their own speculative group.
static int
endpoints(struct pipe *pi, int id, int readers)
{
  int index = spec_index(id), count = 0;
  for (int i = 0; i <= NSPEC; i++) {
    count += readers ? pi->readers[i] : pi->writers[i];
    if (i && i != index)
      count += readers ? pi->savedreaders[i] : pi->savedwriters[i];
  }
  return count;
}

void
pipehold(struct pipe *pi)
{
  acquire(&pi->lock);
  pi->holds++;
  release(&pi->lock);
}

void
pipeunhold(struct pipe *pi)
{
  acquire(&pi->lock);
  pi->holds--;
  int free = !pi->holds && !pi->readopen && !pi->writeopen;
  release(&pi->lock);
  if (free)
    kfree(pi);
}

void
pipespecfinish(struct pipe *pi, int id, int commit)
{
  acquire(&pi->lock);
  uint count = pi->nwrite - pi->nread, destination = pi->nread;
  for (uint i = 0; i < count; i++) {
    uint source = (pi->nread + i) % PIPESIZE;
    if (pi->tag[source] == id && !commit)
      continue;
    uint dst = destination++ % PIPESIZE;
    pi->data[dst] = pi->data[source];
    pi->tag[dst] = pi->tag[source] == id ? 0 : pi->tag[source];
  }
  pi->nwrite = destination;
  wakeup(&pi->nread);
  wakeup(&pi->nwrite);
  release(&pi->lock);
}

static int
pipewrite_data(struct pipe *pi, uint64 addr, int n, int user_src)
{
  int i = 0;
  struct proc *pr = myproc();
  acquire(&pi->lock);
  while (i < n) {
    if (!endpoints(pi, pr->spec, 1) || killed(pr) || spec_failed()) {
      release(&pi->lock);
      return -1;
    }
    if (pi->nwrite == pi->nread + PIPESIZE) {
      wakeup(&pi->nread);
      sleep_prepare(&pi->nwrite);
      release(&pi->lock);
      sleep();
      acquire(&pi->lock);
    } else {
      char ch;
      if (either_copyin(&ch, user_src, addr + i, 1) < 0) {
        if (!i)
          i = -1;
        break;
      }
      uint offset = pi->nwrite++ % PIPESIZE;
      pi->data[offset] = ch;
      pi->tag[offset] = pr->spec;
      i++;
    }
  }
  wakeup(&pi->nread);
  release(&pi->lock);
  return i;
}

int
pipewrite_kernel(struct pipe *pi, char *data, int n)
{
  return pipewrite_data(pi, (uint64)data, n, 0);
}

int
pipewrite(struct pipe *pi, uint64 addr, int n)
{
  if (n < 0)
    return -1;
  if (myproc()->spec) {
    int index = spec_index(myproc()->spec), inside, outside = 0;
    acquire(&pi->lock);
    inside = pi->readers[index];
    for (int j = 0; j <= NSPEC; j++)
      if (j != index)
        outside += pi->readers[j] + pi->savedreaders[j];
    release(&pi->lock);
    if (inside && outside) {
      spec_fail();
      return -1;
    }
    if (!inside)
      return spec_output(pi, 1, addr, n);
    if (spec_track_pipe(pi) < 0)
      return -1;
  }
  return pipewrite_data(pi, addr, n, 1);
}

int
piperead(struct pipe *pi, uint64 addr, int n)
{
  int i;
  struct proc *pr = myproc();
  acquire(&pi->lock);
  while ((pi->nread == pi->nwrite ||
          (pi->tag[pi->nread % PIPESIZE] && pi->tag[pi->nread % PIPESIZE] != pr->spec)) &&
         (endpoints(pi, pr->spec, 0) || pi->pending)) {
    if (killed(pr) || spec_failed()) {
      release(&pi->lock);
      return -1;
    }
    sleep_prepare(&pi->nread);
    release(&pi->lock);
    sleep();
    acquire(&pi->lock);
  }
  for (i = 0; i < n; i++) {
    if (pi->nread == pi->nwrite ||
        (pi->tag[pi->nread % PIPESIZE] && pi->tag[pi->nread % PIPESIZE] != pr->spec))
      break;
    char ch = pi->data[pi->nread % PIPESIZE];
    if (copyout(pr->pagetable, pr->sz, addr + i, &ch, 1) < 0) {
      if (!i)
        i = -1;
      break;
    }
    pi->nread++;
  }
  wakeup(&pi->nwrite);
  release(&pi->lock);
  return i;
}
