// Independent, read-only on-disk allocation check for a quiescent shadow VM.
// This is not a process speculation syscall: isolation is the private VM disk.
#include "types.h"
#include "riscv.h"
#include "defs.h"
#include "param.h"
#include "spinlock.h"
#include "sleeplock.h"
#include "fs.h"
#include "buf.h"
#include "stat.h"

extern struct superblock sb;
extern uint64 sys_sync(void);

static int
claim(uchar *seen, uint block, uint start)
{
  if (block < start || block >= sb.size ||
      (seen[block / 8] & (1 << (block % 8))))
    return -1;
  seen[block / 8] |= 1 << (block % 8);
  return 0;
}

static int
check_inode(uchar *seen, struct dinode *ip, uint start)
{
  uint count = (ip->size + BSIZE - 1) / BSIZE;
  if (ip->type == 0)
    return 0;
  if ((ip->type != T_DIR && ip->type != T_FILE && ip->type != T_DEVICE) ||
      ip->nlink < 1 || ip->size > MAXFILE * BSIZE)
    return -1;
  if (ip->type == T_DEVICE && ip->size != 0)
    return -1;
  for (uint i = 0; i < NDIRECT; i++) {
    if (i < count) {
      if (claim(seen, ip->addrs[i], start) < 0)
        return -1;
    } else if (ip->addrs[i]) {
      return -1;
    }
  }
  if (count > NDIRECT) {
    if (claim(seen, ip->addrs[NDIRECT], start) < 0)
      return -1;
    struct buf *bp = bread(ROOTDEV, ip->addrs[NDIRECT]);
    uint *blocks = (uint *)bp->data;
    int valid = 1;
    for (uint i = 0; i < NINDIRECT; i++) {
      if (i < count - NDIRECT) {
        if (claim(seen, blocks[i], start) < 0)
          valid = 0;
      } else if (blocks[i]) {
        valid = 0;
      }
    }
    brelse(bp);
    if (!valid)
      return -1;
  } else if (ip->addrs[NDIRECT]) {
    return -1;
  }
  return 0;
}

uint64
sys_shadowcheck(void)
{
  // shadowd is the only mutator in this dedicated VM. The parent shell waits.
  // Never attach a live/shared disk to this VM.
  sys_sync();
  if (sb.magic != FSMAGIC || sb.size > PGSIZE * 8 || sb.nblocks >= sb.size ||
      sb.ninodes < 2 || sb.ninodes > 4096 || sb.logstart != 2 || sb.nlog < 2 ||
      sb.inodestart != sb.logstart + sb.nlog ||
      sb.bmapstart < sb.inodestart + (sb.ninodes + IPB - 1) / IPB)
    return -1;
  uint start = sb.size - sb.nblocks;
  if (start >= sb.size || start < sb.bmapstart + (sb.size + BPB - 1) / BPB)
    return -1;
  uchar *seen = (uchar *)kalloc();
  if (!seen)
    return -1;
  memset(seen, 0, PGSIZE);
  for (uint b = 0; b < start; b++)
    seen[b / 8] |= 1 << (b % 8);
  int valid = 1;
  for (uint ino = 1; ino < sb.ninodes && valid; ino++) {
    struct buf *bp = bread(ROOTDEV, IBLOCK(ino, sb));
    struct dinode inode = ((struct dinode *)bp->data)[ino % IPB];
    brelse(bp);
    if (check_inode(seen, &inode, start) < 0)
      valid = 0;
  }
  for (uint b = 0; b < sb.size && valid; b += BPB) {
    struct buf *bp = bread(ROOTDEV, BBLOCK(b, sb));
    for (uint i = 0; i < BPB && b + i < sb.size; i++) {
      int actual = (bp->data[i / 8] >> (i % 8)) & 1;
      int expected = (seen[(b + i) / 8] >> ((b + i) % 8)) & 1;
      if (actual != expected)
        valid = 0;
    }
    brelse(bp);
  }
  struct buf *bp = bread(ROOTDEV, sb.logstart);
  if (*(int *)bp->data != 0)
    valid = 0;
  brelse(bp);
  kfree(seen);
  return valid ? 0 : -1;
}
