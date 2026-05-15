"""
企微智能机器人多媒体资源解密。

官方规范(developer.work.weixin.qq.com/document/path/101463):
- 加密方式: AES-256-CBC
- PKCS#7 padding, 填充至 32 字节的倍数
- aeskey 是 base64 编码,decode 后 32 字节,整个作为 AES-256 key
- IV = decoded_aeskey[:16] (前 16 字节)
- 每个 url 对应的 aeskey 唯一
"""
from __future__ import annotations

import base64

from Crypto.Cipher import AES


class AESDecryptError(Exception):
    """解密失败的统一异常"""


def _lenient_pkcs7_unpad(data: bytes, max_pad: int = 32) -> bytes:
    """
    宽容版 PKCS#7 unpad,不做 len(data) % block_size 检查,只看末尾字节。

    pycryptodome 的 Crypto.Util.Padding.unpad 会强制 len(data) 是 block_size 倍数,
    block_size=16 → 拒绝 padding count > 16 的情况
    block_size=32 → 拒绝长度不是 32 倍数的密文
    两种都不够灵活。本函数只验证:
    1. 最后一字节 n 在 [1, max_pad]
    2. 末尾 n 字节都是值 n
    可同时兼容 16 字节倍数填充和 32 字节倍数填充。
    """
    if not data:
        raise ValueError("Zero-length input cannot be unpadded")
    n = data[-1]
    if n < 1 or n > max_pad:
        raise ValueError(f"padding length {n} out of range [1, {max_pad}]")
    if len(data) < n:
        raise ValueError(f"data length {len(data)} < claimed padding {n}")
    if data[-n:] != bytes([n]) * n:
        raise ValueError(f"last {n} bytes are not all 0x{n:02x}")
    return data[:-n]


def decrypt(ciphertext: bytes, aeskey_b64: str) -> bytes:
    """
    用 base64 编码的 aeskey 解密 AES-256-CBC 密文,返回原始明文字节。

    Args:
        ciphertext: 从 url 下载到的加密文件字节流
        aeskey_b64: body['file']['aeskey'] 或 body['image']['aeskey']

    Raises:
        AESDecryptError: aeskey 长度不对、密文长度非 16 倍数、padding 不合法等
    """
    if not aeskey_b64:
        raise AESDecryptError("aeskey 为空")
    if not ciphertext:
        raise AESDecryptError("密文为空")

    try:
        # 企微 aeskey 实测 43 字符(32 字节内容),少一个 padding '=' 结尾
        # b64decode 严格,得自己补齐 4 倍数
        padded = aeskey_b64 + "=" * (-len(aeskey_b64) % 4)
        key = base64.b64decode(padded)
    except Exception as e:
        raise AESDecryptError(f"aeskey 不是合法的 base64: {e}") from e

    if len(key) != 32:
        raise AESDecryptError(f"aeskey decode 后期望 32 字节,实际 {len(key)} 字节")

    if len(ciphertext) % 16 != 0:
        raise AESDecryptError(
            f"密文长度 {len(ciphertext)} 不是 16 的倍数,AES-CBC 无法解密"
        )

    iv = key[:16]
    cipher = AES.new(key, AES.MODE_CBC, iv)

    try:
        padded_plaintext = cipher.decrypt(ciphertext)
    except ValueError as e:
        raise AESDecryptError(f"AES 解密失败: {e}") from e

    # PKCS#7 unpad,max_pad=32 —— 用自己写的宽容版本以兼容企微两种填充倍数
    try:
        plaintext = _lenient_pkcs7_unpad(padded_plaintext, max_pad=32)
    except ValueError as e:
        raise AESDecryptError(f"PKCS#7 unpad 失败,可能 aeskey 错误或密文损坏: {e}") from e

    return plaintext
