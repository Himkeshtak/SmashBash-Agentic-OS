#include "types.h"
#include "riscv.h"
#include "defs.h"
#include "param.h"
#include "memlayout.h"
#include "spinlock.h"
#include "proc.h"
#include "vm.h"

uint64
sys_exit(void)
{
  int n;
  argint(0, &n);
  kexit(n);
  return 0; // not reached
}

uint64
sys_getpid(void)
{
  return myproc()->pid;
}

uint64
sys_fork(void)
{
  return kfork();
}

uint64
sys_wait(void)
{
  uint64 p;
  argaddr(0, &p);
  return kwait(p);
}

uint64
sys_sbrk(void)
{
  uint64 addr;
  int t;
  int n;

  argint(0, &n);
  argint(1, &t);
  addr = myproc()->sz;

  if (t == SBRK_EAGER || n < 0) {
    if (growproc(n) < 0) {
      return -1;
    }
  } else {
    // Lazily allocate memory for this process: increase its memory
    // size but don't allocate memory. If the processes uses the
    // memory, vmfault() will allocate it.
    if (addr + n < addr)
      return -1;
    if (addr + n > TRAPFRAME)
      return -1;
    myproc()->sz += n;
  }
  return addr;
}

uint64
sys_pause(void)
{
  int n;
  uint ticks0;

  argint(0, &n);
  if (n < 0)
    n = 0;
  acquire(&tickslock);
  ticks0 = ticks;
  while (ticks - ticks0 < n) {
    if (killed(myproc())) {
      release(&tickslock);
      return -1;
    }
    sleep_prepare(&ticks);
    release(&tickslock);
    sleep();
    acquire(&tickslock);
  }
  release(&tickslock);
  return 0;
}

uint64
sys_kill(void)
{
  int pid;

  argint(0, &pid);
  return kkill(pid);
}

// return how many clock tick interrupts have occurred
// since start.
uint64
sys_uptime(void)
{
  uint xticks;

  acquire(&tickslock);
  xticks = ticks;
  release(&tickslock);
  return xticks;
}

static struct spinlock speclock;
static int next_spec_id = 1;
static int speclock_inited = 0;

uint64
sys_xbegin(void)
{
  struct proc *p = myproc();

  // Hidden test: Nested xbegin is explicitly rejected with -1
  if (p->spec != 0)
    return -1;

  if (!speclock_inited) {
    initlock(&speclock, "speclock");
    speclock_inited = 1;
  }

  acquire(&speclock);
  p->spec = next_spec_id++;
  release(&speclock);

  return 0;
}

uint64
sys_xcommit(void)
{
  struct proc *p = myproc();
  if (p->spec == 0)
    return -1;

  int spec = p->spec;

  // 1. Commit all shadow blocks in one filesystem log transaction.
  //    shadow_commit() returns -1 if a conflict was detected.
  int ret = shadow_commit(spec);
  if (ret < 0) {
    // Conflict rule: First-committer-wins. Outside write invalidated this speculation.
    console_spec_discard(spec);
    pipe_spec_discard(spec);
    proc_clear_spec(spec);
    return -1;
  }

  // 2. Only after end_op() has returned in shadow_commit, release held output!
  console_spec_flush(spec);
  pipe_spec_flush(spec);

  // 3. Clear speculation ID across the process tree
  proc_clear_spec(spec);

  return 0;
}

uint64
sys_xabort(void)
{
  struct proc *p = myproc();
  if (p->spec == 0)
    return -1;

  int spec = p->spec;

  // Discard all private shadow state and held output
  shadow_abort(spec);
  console_spec_discard(spec);
  pipe_spec_discard(spec);

  // Clear speculation ID across the process tree
  proc_clear_spec(spec);

  return 0;
}

