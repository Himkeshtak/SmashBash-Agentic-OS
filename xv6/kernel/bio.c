// Buffer cache with speculative in-kernel shadow block redirection.
//
// The buffer cache is a linked list of buf structures holding
// cached copies of disk block contents.
// For speculative processes (p->spec > 0), writes are intercepted
// into private shadow blocks (shadow_table.shadows).
// Outside processes (p->spec == 0) never see shadow blocks.

#include "types.h"
#include "param.h"
#include "spinlock.h"
#include "sleeplock.h"
#include "riscv.h"
#include "defs.h"
#include "fs.h"
#include "buf.h"
#include "proc.h"

struct {
  struct spinlock lock;
  struct buf buf[NBUF];

  // Linked list of all buffers, through prev/next.
  // Sorted by how recently the buffer was used.
  // head.next is most recent, head.prev is least.
  struct buf head;
} bcache;

// In-kernel Shadow Block Table
struct shadow {
  int spec;          // Which speculation session owns this copy; 0 = free
  uint dev;          // Device number
  uint blockno;      // Disk block number
  struct buf buf;    // Dedicated struct buf for this shadow block
};

static struct {
  struct spinlock lock;
  struct shadow shadows[NSHADOW];
} shadow_table;

// Conflict tracking for First-Committer-Wins policy
static int spec_conflicted[NPROC];

void
binit(void)
{
  struct buf *b;

  initlock(&bcache.lock, "bcache");

  // Create linked list of buffers
  bcache.head.prev = &bcache.head;
  bcache.head.next = &bcache.head;
  for (b = bcache.buf; b < bcache.buf + NBUF; b++) {
    b->next = bcache.head.next;
    b->prev = &bcache.head;
    initsleeplock(&b->lock, "buffer");
    bcache.head.next->prev = b;
    bcache.head.next = b;
  }

  // Initialize shadow table
  initlock(&shadow_table.lock, "shadows");
  for (int i = 0; i < NSHADOW; i++) {
    shadow_table.shadows[i].spec = 0;
    shadow_table.shadows[i].dev = 0;
    shadow_table.shadows[i].blockno = 0;
    initsleeplock(&shadow_table.shadows[i].buf.lock, "shadow_buf");
    shadow_table.shadows[i].buf.refcnt = 0;
    shadow_table.shadows[i].buf.valid = 0;
    shadow_table.shadows[i].buf.disk = 0;
  }

  for (int i = 0; i < NPROC; i++) {
    spec_conflicted[i] = 0;
  }
}

// Check if a buffer pointer belongs to the shadow table
int
is_shadow_buf(struct buf *b)
{
  return (b >= &shadow_table.shadows[0].buf &&
          b <= &shadow_table.shadows[NSHADOW - 1].buf);
}

// Check if speculation has been marked conflicted
int
is_spec_conflicted(int spec)
{
  if (spec <= 0 || spec >= NPROC)
    return 0;
  return spec_conflicted[spec];
}

void
clear_spec_conflict(int spec)
{
  if (spec > 0 && spec < NPROC)
    spec_conflicted[spec] = 0;
}

// Record conflict when an outside process modifies a block held in shadows
void
shadow_check_conflict(uint dev, uint blockno)
{
  acquire(&shadow_table.lock);
  for (int i = 0; i < NSHADOW; i++) {
    if (shadow_table.shadows[i].spec > 0 &&
        shadow_table.shadows[i].dev == dev &&
        shadow_table.shadows[i].blockno == blockno) {
      int s = shadow_table.shadows[i].spec;
      if (s < NPROC)
        spec_conflicted[s] = 1;
    }
  }
  release(&shadow_table.lock);
}

// Look through buffer cache for block on device dev.
// If not found, allocate a buffer.
// In either case, return locked buffer.
static struct buf *
bget(uint dev, uint blockno)
{
  struct buf *b;

  acquire(&bcache.lock);

  // Is the block already cached?
  for (b = bcache.head.next; b != &bcache.head; b = b->next) {
    if (b->dev == dev && b->blockno == blockno) {
      b->refcnt++;
      release(&bcache.lock);
      acquiresleep(&b->lock);
      return b;
    }
  }

  // Not cached.
  // Recycle the least recently used (LRU) unused buffer.
  for (b = bcache.head.prev; b != &bcache.head; b = b->prev) {
    if (b->refcnt == 0) {
      b->dev = dev;
      b->blockno = blockno;
      b->valid = 0;
      b->refcnt = 1;
      release(&bcache.lock);
      acquiresleep(&b->lock);
      return b;
    }
  }
  panic("bget: no buffers");
}

// Internal real disk read
static struct buf *
real_bread(uint dev, uint blockno)
{
  struct buf *b = bget(dev, blockno);
  if (!b->valid) {
    virtio_disk_rw(b, 0);
    b->valid = 1;
  }
  return b;
}

// Find existing shadow block for a speculation
static struct shadow *
shadow_find(int spec, uint dev, uint blockno)
{
  acquire(&shadow_table.lock);
  for (int i = 0; i < NSHADOW; i++) {
    if (shadow_table.shadows[i].spec == spec &&
        shadow_table.shadows[i].dev == dev &&
        shadow_table.shadows[i].blockno == blockno) {
      release(&shadow_table.lock);
      return &shadow_table.shadows[i];
    }
  }
  release(&shadow_table.lock);
  return 0;
}

// Return locked struct buf for a shadow entry
static struct buf *
shadow_buf(struct shadow *s)
{
  acquiresleep(&s->buf.lock);
  acquire(&shadow_table.lock);
  s->buf.refcnt++;
  release(&shadow_table.lock);
  return &s->buf;
}

// Return a locked buf with the contents of the indicated block.
// Speculative processes (p->spec > 0) see their own shadow copy if present.
// Outside processes (p->spec == 0) fall straight through to real disk.
struct buf *
bread(uint dev, uint blockno)
{
  struct proc *p = myproc();
  if (p && p->spec > 0) {
    struct shadow *s = shadow_find(p->spec, dev, blockno);
    if (s) {
      return shadow_buf(s); // our own earlier write
    }
  }
  return real_bread(dev, blockno);
}

// Retrieve buffer for WRITING.
// If caller is speculative, allocates a private shadow slot if not already shadowed.
// If shadow table is full, returns 0 (fails operation with -1, never externalizes!).
// If outside process, marks conflict if block is in shadows and returns real buf.
struct buf *
bread_write(uint dev, uint blockno)
{
  struct proc *p = myproc();
  if (p && p->spec > 0) {
    // 1. Check if already shadowed
    struct shadow *s = shadow_find(p->spec, dev, blockno);
    if (s) {
      return shadow_buf(s);
    }

    // 2. Allocate a free shadow slot
    acquire(&shadow_table.lock);
    int free_slot = -1;
    for (int i = 0; i < NSHADOW; i++) {
      if (shadow_table.shadows[i].spec == 0) {
        free_slot = i;
        break;
      }
    }
    if (free_slot == -1) {
      // Out of shadow blocks! Must fail with NULL / -1.
      // Falling back to real disk would externalize.
      release(&shadow_table.lock);
      return 0;
    }

    s = &shadow_table.shadows[free_slot];
    s->spec = p->spec;
    s->dev = dev;
    s->blockno = blockno;
    s->buf.dev = dev;
    s->buf.blockno = blockno;
    s->buf.valid = 1;
    s->buf.refcnt = 1;
    release(&shadow_table.lock);

    acquiresleep(&s->buf.lock);

    // Copy initial data from real disk into shadow buffer
    struct buf *real_bp = real_bread(dev, blockno);
    memmove(s->buf.data, real_bp->data, BSIZE);
    brelse(real_bp);

    return &s->buf;
  }

  // Outside process: check for conflict with active speculations
  shadow_check_conflict(dev, blockno);
  return real_bread(dev, blockno);
}

// Write b's contents to disk. Must be locked.
// Only the log calls bwrite.
void
bwrite(struct buf *b)
{
  if (!holdingsleep(&b->lock))
    panic("bwrite");
  virtio_disk_rw(b, 1);
}

// Release a locked buffer.
void
brelse(struct buf *b)
{
  if (!holdingsleep(&b->lock))
    panic("brelse");

  releasesleep(&b->lock);

  // If shadow buffer, update refcnt without touching real bcache LRU
  if (is_shadow_buf(b)) {
    acquire(&shadow_table.lock);
    b->refcnt--;
    release(&shadow_table.lock);
    return;
  }

  acquire(&bcache.lock);
  b->refcnt--;
  if (b->refcnt == 0) {
    // no one is waiting for it.
    b->next->prev = b->prev;
    b->prev->next = b->next;
    b->next = bcache.head.next;
    b->prev = &bcache.head;
    bcache.head.next->prev = b;
    bcache.head.next = b;
  }
  release(&bcache.lock);
}

void
bpin(struct buf *b)
{
  if (is_shadow_buf(b)) {
    acquire(&shadow_table.lock);
    b->refcnt++;
    release(&shadow_table.lock);
    return;
  }
  acquire(&bcache.lock);
  b->refcnt++;
  release(&bcache.lock);
}

void
bunpin(struct buf *b)
{
  if (is_shadow_buf(b)) {
    acquire(&shadow_table.lock);
    b->refcnt--;
    release(&shadow_table.lock);
    return;
  }
  acquire(&bcache.lock);
  b->refcnt--;
  release(&bcache.lock);
}

// Free all shadow slots belonging to a speculation (used by xabort)
void
shadow_abort(int spec)
{
  acquire(&shadow_table.lock);
  for (int i = 0; i < NSHADOW; i++) {
    if (shadow_table.shadows[i].spec == spec) {
      shadow_table.shadows[i].spec = 0;
      shadow_table.shadows[i].dev = 0;
      shadow_table.shadows[i].blockno = 0;
      shadow_table.shadows[i].buf.valid = 0;
      shadow_table.shadows[i].buf.refcnt = 0;
    }
  }
  release(&shadow_table.lock);
  clear_spec_conflict(spec);
}

// Commit all shadow slots for a speculation into the on-disk log
// in one atomic log transaction. Returns 0 on success, -1 on conflict.
int
shadow_commit(int spec)
{
  if (is_spec_conflicted(spec)) {
    shadow_abort(spec);
    return -1;
  }

  begin_op();
  acquire(&shadow_table.lock);
  for (int i = 0; i < NSHADOW; i++) {
    if (shadow_table.shadows[i].spec == spec) {
      uint dev = shadow_table.shadows[i].dev;
      uint blockno = shadow_table.shadows[i].blockno;

      release(&shadow_table.lock);

      // Write home: read real buffer, copy shadow data, stage into log
      struct buf *bp = real_bread(dev, blockno);
      acquire(&shadow_table.lock);
      memmove(bp->data, shadow_table.shadows[i].buf.data, BSIZE);
      shadow_table.shadows[i].spec = 0; // Free shadow slot
      release(&shadow_table.lock);

      log_write(bp);
      brelse(bp);

      acquire(&shadow_table.lock);
    }
  }
  release(&shadow_table.lock);
  end_op(); // Atomic disk transaction commits here

  clear_spec_conflict(spec);
  return 0;
}

