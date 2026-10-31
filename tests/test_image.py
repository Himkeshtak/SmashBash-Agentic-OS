import struct
import tempfile
import unittest
from pathlib import Path

from smashbash.filesystem import Node
from smashbash.image import BSIZE, Image
from smashbash.model import TransactionError
from tests.helpers import make_image


class ImageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "device.img"
        self.tree = {".": Node("directory"), "note": Node("file", b"abc\0\xff")}
        make_image(self.path, self.tree)

    def tearDown(self):
        self.temp.cleanup()

    def mutate(self, offset, data):
        raw = bytearray(self.path.read_bytes())
        raw[offset:offset + len(data)] = data
        self.path.write_bytes(raw)

    def test_valid_image_tree_and_protected_namespace(self):
        image = Image(self.path)
        self.assertEqual(image.tree("/data"), self.tree)
        self.assertIn("/README", image.outside("/data"))

    def test_malformed_geometry_unclean_log_bitmap_and_block_alias(self):
        valid = self.path.read_bytes()
        mutations = [(BSIZE, b"bad!"), (2 * BSIZE, struct.pack("<I", 1)),
                     (6 * BSIZE, b"\0"),
                     # Inode 4 (managed note) illegally aliases the README inode's block.
                     (4 * BSIZE + 4 * 64 + 12, valid[4 * BSIZE + 3 * 64 + 12:4 * BSIZE + 3 * 64 + 16])]
        for offset, data in mutations:
            self.path.write_bytes(valid)
            self.mutate(offset, data)
            with self.subTest(offset=offset), self.assertRaises(TransactionError):
                Image(self.path)

    def test_directory_cycle_and_bad_link_count(self):
        valid = self.path.read_bytes()
        # data directory's first dot entry must point to itself (inode 2).
        pointer = struct.unpack_from("<I", valid, 4 * BSIZE + 2 * 64 + 12)[0]
        self.mutate(pointer * BSIZE, struct.pack("<H", 1))
        with self.assertRaises(TransactionError):
            Image(self.path)
        self.path.write_bytes(valid)
        self.mutate(4 * BSIZE + 4 * 64 + 6, struct.pack("<h", 2))
        with self.assertRaises(TransactionError):
            Image(self.path)
