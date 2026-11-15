//
// Console input and output, to the uart.
// Reads are line at a time.
// Implements special input characters:
//   newline -- end of line
//   control-h -- backspace
//   control-u -- kill line
//   control-d -- end of file
//   control-p -- print process list
//

#include <stdarg.h>

#include "types.h"
#include "param.h"
#include "spinlock.h"
#include "sleeplock.h"
#include "fs.h"
#include "file.h"
#include "memlayout.h"
#include "riscv.h"
#include "defs.h"
#include "proc.h"

#define BACKSPACE 0x100       // erase the last output character
#define C(x)      ((x) - '@') // Control-x

//
// send one character to the uart, but don't use
// interrupts or sleep(). safe to be called from
// interrupts, e.g. by printk and to echo input
// characters.
//
void
consputc(int c)
{
  if (c == BACKSPACE) {
    // if the user typed backspace, overwrite with a space.
    uartputc_sync('\b');
    uartputc_sync(' ');
    uartputc_sync('\b');
  } else {
    uartputc_sync(c);
  }
}

struct {
  struct spinlock lock;

  // input circular buffer
#define INPUT_BUF_SIZE 512 // Bounded SB1 frames include hex-encoded paths.
  char buf[INPUT_BUF_SIZE];
  uint r; // Read index
  uint w; // Write index
  uint e; // Edit index
} cons;

#define SPEC_CONSOLE_BUF_SIZE 4096

struct spec_console {
  int spec;
  int len;
  char buf[SPEC_CONSOLE_BUF_SIZE];
};

static struct {
  struct spinlock lock;
  struct spec_console cons[NPROC];
} spec_console_table;

void
console_spec_init(void)
{
  initlock(&spec_console_table.lock, "spec_console");
  for (int i = 0; i < NPROC; i++) {
    spec_console_table.cons[i].spec = 0;
    spec_console_table.cons[i].len = 0;
  }
}

void
console_spec_flush(int spec)
{
  acquire(&spec_console_table.lock);
  for (int i = 0; i < NPROC; i++) {
    if (spec_console_table.cons[i].spec == spec) {
      for (int j = 0; j < spec_console_table.cons[i].len; j++) {
        consputc(spec_console_table.cons[i].buf[j]);
      }
      spec_console_table.cons[i].spec = 0;
      spec_console_table.cons[i].len = 0;
      break;
    }
  }
  release(&spec_console_table.lock);
}

void
console_spec_discard(int spec)
{
  acquire(&spec_console_table.lock);
  for (int i = 0; i < NPROC; i++) {
    if (spec_console_table.cons[i].spec == spec) {
      spec_console_table.cons[i].spec = 0;
      spec_console_table.cons[i].len = 0;
      break;
    }
  }
  release(&spec_console_table.lock);
}

//
// user write() system calls to the console go here.
// uses sleep() and UART interrupts.
//
int
consolewrite(int user_src, uint64 src, int n)
{
  struct proc *p = myproc();
  if (p && p->spec > 0) {
    acquire(&spec_console_table.lock);
    struct spec_console *sc = 0;
    for (int i = 0; i < NPROC; i++) {
      if (spec_console_table.cons[i].spec == p->spec) {
        sc = &spec_console_table.cons[i];
        break;
      }
    }
    if (!sc) {
      for (int i = 0; i < NPROC; i++) {
        if (spec_console_table.cons[i].spec == 0) {
          sc = &spec_console_table.cons[i];
          sc->spec = p->spec;
          sc->len = 0;
          break;
        }
      }
    }
    if (sc) {
      char c;
      for (int i = 0; i < n && sc->len < SPEC_CONSOLE_BUF_SIZE; i++) {
        if (either_copyin(&c, user_src, src + i, 1) == -1)
          break;
        sc->buf[sc->len++] = c;
      }
    }
    release(&spec_console_table.lock);
    return n;
  }

  char buf[32]; // move batches from user space to uart.
  int i = 0;

  while (i < n) {
    int nn = sizeof(buf);
    if (nn > n - i)
      nn = n - i;
    if (either_copyin(buf, user_src, src + i, nn) == -1)
      break;
    uartwrite(buf, nn);
    i += nn;
  }

  return i;
}

//
// user read()s from the console go here.
// copy (up to) a whole input line to dst.
// user_dst indicates whether dst is a user
// or kernel address.
//
int
consoleread(int user_dst, uint64 dst, int n)
{
  uint target;
  int c;
  char cbuf;

  target = n;
  acquire(&cons.lock);
  while (n > 0) {
    // wait until interrupt handler has put some
    // input into cons.buffer.
    while (cons.r == cons.w) {
      if (killed(myproc())) {
        release(&cons.lock);
        return -1;
      }
      sleep_prepare(&cons.r);
      release(&cons.lock);
      sleep();
      acquire(&cons.lock);
    }

    c = cons.buf[cons.r++ % INPUT_BUF_SIZE];

    if (c == C('D')) { // end-of-file
      if (n < target) {
        // Save ^D for next time, to make sure
        // caller gets a 0-byte result.
        cons.r--;
      }
      break;
    }

    // copy the input byte to the user-space buffer.
    cbuf = c;
    if (either_copyout(user_dst, dst, &cbuf, 1) == -1)
      break;

    dst++;
    --n;

    if (c == '\n') {
      // a whole line has arrived, return to
      // the user-level read().
      break;
    }
  }
  release(&cons.lock);

  return target - n;
}

//
// the console input interrupt handler.
// uartintr() calls this for each input character.
// do erase/kill processing, append to cons.buf,
// wake up consoleread() if a whole line has arrived.
//
void
consoleintr(int c)
{
  acquire(&cons.lock);

  switch (c) {
  case C('P'): // Print process list.
    procdump();
    break;
  case C('U'): // Kill line.
    while (cons.e != cons.w &&
           cons.buf[(cons.e - 1) % INPUT_BUF_SIZE] != '\n') {
      cons.e--;
      consputc(BACKSPACE);
    }
    break;
  case C('H'): // Backspace
  case '\x7f': // Delete key
    if (cons.e != cons.w) {
      cons.e--;
      consputc(BACKSPACE);
    }
    break;
  default:
    if (c != 0 && cons.e - cons.r < INPUT_BUF_SIZE) {
      c = (c == '\r') ? '\n' : c;

      // echo back to the user.
      consputc(c);

      // store for consumption by consoleread().
      cons.buf[cons.e++ % INPUT_BUF_SIZE] = c;

      if (c == '\n' || c == C('D') || cons.e - cons.r == INPUT_BUF_SIZE) {
        // wake up consoleread() if a whole line (or end-of-file)
        // has arrived.
        cons.w = cons.e;
        wakeup(&cons.r);
      }
    }
    break;
  }

  release(&cons.lock);
}

void
consoleinit(void)
{
  initlock(&cons.lock, "cons");
  console_spec_init();

  uartinit();

  // connect read and write system calls
  // to consoleread and consolewrite.
  devsw[CONSOLE].read = consoleread;
  devsw[CONSOLE].write = consolewrite;
}
