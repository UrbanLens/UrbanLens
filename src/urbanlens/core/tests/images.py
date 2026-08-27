"""Real image bytes for tests that upload one.

Uploading `b"photo-bytes"` named `photo.jpg` used to work, because content
sniffing failed open on anything `filetype` could not fingerprint. Photos now
require a positive identification - a file whose bytes are not an image is
refused - so a test that wants to exercise a successful upload has to supply an
actual image.

That is a better test regardless of the check: a placeholder string is not
something the product would ever accept, so a test built on one was describing
a path no user can take.

Each helper returns bytes for the smallest valid file of its format. They are
literals rather than generated with Pillow so that these stay usable in a
``SimpleTestCase`` and cost nothing to construct.
"""

from __future__ import annotations

#: A 1x1 PNG. Produced by Pillow and confirmed to fingerprint as ``png``.
PNG_BYTES = bytes.fromhex("89504e470d0a1a0a0000000d4948445200000001000000010802000000907753de0000000c49444154789c63f8ffff3f0005fe02fe0def46b80000000049454e44ae426082")

#: A 1x1 JPEG. Larger than the others because a JPEG carries its quantisation
#: and Huffman tables; a hand-trimmed one is not a JPEG any decoder will open.
JPEG_BYTES = bytes.fromhex(
    "ffd8ffe000104a46494600010100000100010000ffdb004300080606070605080707070909080a0c140d0c0b0b0c19"
    "12130f141d1a1f1e1d1a1c1c20242e2720222c231c1c2837292c30313434341f27393d38323c2e333432ffdb004301"
    "0909090c0b0c180d0d1832211c2132323232323232323232323232323232323232323232323232323232323232323"
    "2323232323232323232323232323232ffc00011080001000103012200021101031101ffc4001f000001050101010101"
    "0100000000000000000102030405060708090a0bffc400b5100002010303020403050504040000017d010203000411"
    "05122131410613516107227114328191a1082342b1c11552d1f02433627282090a161718191a25262728292a343536"
    "3738393a434445464748494a535455565758595a636465666768696a737475767778797a838485868788898a929394"
    "95969798999aa2a3a4a5a6a7a8a9aab2b3b4b5b6b7b8b9bac2c3c4c5c6c7c8c9cad2d3d4d5d6d7d8d9dae1e2e3e4e5"
    "e6e7e8e9eaf1f2f3f4f5f6f7f8f9faffc4001f0100030101010101010101010000000000000102030405060708090a"
    "0bffc400b51100020102040403040705040400010277000102031104052131061241510761711322328108144291a1"
    "b1c109233352f0156272d10a162434e125f11718191a262728292a35363738393a434445464748494a535455565758"
    "595a636465666768696a737475767778797a82838485868788898a92939495969798999aa2a3a4a5a6a7a8a9aab2b3"
    "b4b5b6b7b8b9bac2c3c4c5c6c7c8c9cad2d3d4d5d6d7d8d9dae2e3e4e5e6e7e8e9eaf2f3f4f5f6f7f8f9faffda000c"
    "03010002110311003f00f7fa28a2803fffd9"
)

#: A 1x1 GIF.
GIF_BYTES = bytes.fromhex("474946383761010001008000000000000000002c000000000100010000080400010404003b")

#: A 1x1 TIFF. Here because ``filetype`` names it ``tif`` rather than ``tiff``,
#: which is the alias mismatch that would have made strict photo sniffing reject
#: real TIFF uploads - see ``services.security.content_sniffing``.
TIFF_BYTES = bytes.fromhex(
    "49492a00080000000a0000010400010000000100000001010400010000000100000002010300030000008600000003"
    "010300010000000100000006010300010000000200000011010400010000008c000000150103000100000003000000"
    "1601040001000000010000001701040001000000030000001c010300010000000100000000000000080008000800ffffff"
)


def png_upload(name: str = "photo.png"):
    """A ``SimpleUploadedFile`` holding a real PNG.

    Args:
        name: Filename to upload it under. The extension matters - it decides
            the Content-Type the file is later served with, and photo uploads
            are allowlisted by it.

    Returns:
        An uploaded file ready to post.
    """
    from django.core.files.uploadedfile import SimpleUploadedFile

    return SimpleUploadedFile(name, PNG_BYTES, content_type="image/png")


def jpeg_upload(name: str = "photo.jpg"):
    """A ``SimpleUploadedFile`` holding a real JPEG.

    Args:
        name: Filename to upload it under.

    Returns:
        An uploaded file ready to post.
    """
    from django.core.files.uploadedfile import SimpleUploadedFile

    return SimpleUploadedFile(name, JPEG_BYTES, content_type="image/jpeg")
