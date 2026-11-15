#include "types.h"
#include "riscv.h"
#include "defs.h"
#include "param.h"
#include "spinlock.h"
#include "proc.h"
#include "fs.h"
#include "sleeplock.h"
#include "file.h"

#define PIPESIZE 512

struct pipe {
  struct spinlock lock;
  char data[PIPESIZE];
  uint nread;    // number of bytes read
  uint nwrite;   // number of bytes written
  int readopen;  // read fd is still open
  int writeopen; // write fd is still open
};

static int pipe_spec_initialized = 0;

int
pipealloc(struct file **f0, struct file **f1)
{
  struct pipe *pi;

  if (!pipe_spec_initialized) {
    pipe_spec_init();
    pipe_spec_initialized = 1;
  }

  pi = 0;
  *f0 = *f1 = 0;
  if ((*f0 = filealloc()) == 0 || (*f1 = filealloc()) == 0)
    goto bad;
  if ((pi = (struct pipe *)kalloc()) == 0)
    goto bad;
  pi->readopen = 1;
  pi->writeopen = 1;
  pi->nwrite = 0;
  pi->nread = 0;
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
    kfree((char *)pi);
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
  if (pi->readopen == 0 && pi->writeopen == 0) {
    release(&pi->lock);
    kfree((char *)pi);
  } else
    release(&pi->lock);
}

extern struct proc proc[NPROC];

#define SPEC_PIPE_BUF_SIZE 2048

struct spec_pipe {
  int spec;
  struct pipe *pi;
  int len;
  char buf[SPEC_PIPE_BUF_SIZE];
};

static struct {
  struct spinlock lock;
  struct spec_pipe pipes[NPROC];
} spec_pipe_table;

void
pipe_spec_init(void)
{
  initlock(&spec_pipe_table.lock, "spec_pipe");
  for (int i = 0; i < NPROC; i++) {
    spec_pipe_table.pipes[i].spec = 0;
    spec_pipe_table.pipes[i].pi = 0;
    spec_pipe_table.pipes[i].len = 0;
  }
}

// Check if any open reader of this pipe is outside writer_spec
int
pipe_has_outside_reader(struct pipe *pi, int writer_spec)
{
  struct proc *p;
  for (p = proc; p < &proc[NPROC]; p++) {
    if (p->state != UNUSED) {
      for (int fd = 0; fd < NOFILE; fd++) {
        if (p->ofile[fd] && p->ofile[fd]->type == FD_PIPE &&
            p->ofile[fd]->pipe == pi && p->ofile[fd]->readable) {
          if (p->spec != writer_spec) {
            return 1;
          }
        }
      }
    }
  }
  return 0;
}

void
pipe_spec_flush(int spec)
{
  acquire(&spec_pipe_table.lock);
  for (int idx = 0; idx < NPROC; idx++) {
    if (spec_pipe_table.pipes[idx].spec == spec && spec_pipe_table.pipes[idx].pi) {
      struct pipe *pi = spec_pipe_table.pipes[idx].pi;
      int len = spec_pipe_table.pipes[idx].len;
      acquire(&pi->lock);
      for (int j = 0; j < len; j++) {
        if (pi->readopen == 0)
          break;
        pi->data[pi->nwrite++ % PIPESIZE] = spec_pipe_table.pipes[idx].buf[j];
      }
      wakeup(&pi->nread);
      release(&pi->lock);

      spec_pipe_table.pipes[idx].spec = 0;
      spec_pipe_table.pipes[idx].pi = 0;
      spec_pipe_table.pipes[idx].len = 0;
    }
  }
  release(&spec_pipe_table.lock);
}

void
pipe_spec_discard(int spec)
{
  acquire(&spec_pipe_table.lock);
  for (int idx = 0; idx < NPROC; idx++) {
    if (spec_pipe_table.pipes[idx].spec == spec) {
      spec_pipe_table.pipes[idx].spec = 0;
      spec_pipe_table.pipes[idx].pi = 0;
      spec_pipe_table.pipes[idx].len = 0;
    }
  }
  release(&spec_pipe_table.lock);
}

int
pipewrite(struct pipe *pi, uint64 addr, int n)
{
  int i = 0;
  struct proc *pr = myproc();

  // If writer is speculative and has an outside reader, buffer until commit
  if (pr && pr->spec > 0 && pipe_has_outside_reader(pi, pr->spec)) {
    acquire(&spec_pipe_table.lock);
    struct spec_pipe *sp = 0;
    for (int idx = 0; idx < NPROC; idx++) {
      if (spec_pipe_table.pipes[idx].spec == pr->spec &&
          spec_pipe_table.pipes[idx].pi == pi) {
        sp = &spec_pipe_table.pipes[idx];
        break;
      }
    }
    if (!sp) {
      for (int idx = 0; idx < NPROC; idx++) {
        if (spec_pipe_table.pipes[idx].spec == 0) {
          sp = &spec_pipe_table.pipes[idx];
          sp->spec = pr->spec;
          sp->pi = pi;
          sp->len = 0;
          break;
        }
      }
    }
    if (sp) {
      for (int j = 0; j < n && sp->len < SPEC_PIPE_BUF_SIZE; j++) {
        char ch;
        if (copyin(pr->pagetable, pr->sz, &ch, addr + j, 1) == -1)
          break;
        sp->buf[sp->len++] = ch;
        i++;
      }
    }
    release(&spec_pipe_table.lock);
    return i;
  }

  acquire(&pi->lock);
  while (i < n) {
    if (pi->readopen == 0 || killed(pr)) {
      release(&pi->lock);
      return -1;
    }
    if (pi->nwrite == pi->nread + PIPESIZE) { //DOC: pipewrite-full
      wakeup(&pi->nread);
      sleep_prepare(&pi->nwrite);
      release(&pi->lock);
      sleep();
      acquire(&pi->lock);
    } else {
      char ch;
      if (copyin(pr->pagetable, pr->sz, &ch, addr + i, 1) == -1) {
        if (i == 0)
          i = -1;
        break;
      }
      pi->data[pi->nwrite++ % PIPESIZE] = ch;
      i++;
    }
  }
  wakeup(&pi->nread);
  release(&pi->lock);

  return i;
}

int
piperead(struct pipe *pi, uint64 addr, int n)
{
  int i;
  struct proc *pr = myproc();
  char ch;

  acquire(&pi->lock);
  while (pi->nread == pi->nwrite && pi->writeopen) { //DOC: pipe-empty
    if (killed(pr)) {
      release(&pi->lock);
      return -1;
    }
    sleep_prepare(&pi->nread); //DOC: piperead-sleep
    release(&pi->lock);
    sleep();
    acquire(&pi->lock);
  }
  for (i = 0; i < n; i++) { //DOC: piperead-copy
    if (pi->nread == pi->nwrite)
      break;
    ch = pi->data[pi->nread % PIPESIZE];
    if (copyout(pr->pagetable, pr->sz, addr + i, &ch, 1) == -1) {
      if (i == 0)
        i = -1;
      break;
    }
    pi->nread++;
  }
  wakeup(&pi->nwrite); //DOC: piperead-wakeup
  release(&pi->lock);
  return i;
}
