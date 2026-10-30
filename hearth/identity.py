"""A member's identity: two independent key pairs, saved in `identity.key`.

- An Ed25519 signing key. Its hex public key *is* the member's account ID, and
  it signs every log entry and every handshake.
- An X25519 encryption key. It's used only once: to receive the sealed group
  key when joining (Phase 6).

The two are generated separately, following libsodium's advice not to reuse
one key for both jobs; there is no Ed25519-to-Curve25519 conversion anywhere.

Joke: two keys, one person. Like a house key and a bike-lock key, except losing
either one means explaining yourself to a group chat.
"""

from dataclasses import dataclass, field
from pathlib import Path

from nacl.exceptions import BadSignatureError
from nacl.public import PrivateKey
from nacl.signing import SigningKey, VerifyKey

from hearth import canonical, config, keyfile

IDENTITY_FILE_VERSION = 1


@dataclass
class Identity:
    """Both private keys of one member. Never log or print these."""

    signing_key: SigningKey
    encryption_key: PrivateKey
    # Derived from the keys in __post_init__ (plain fields, no @property magic).
    # pubkey is the account ID: the Ed25519 public key as 64 lowercase hex chars.
    pubkey: str = field(init=False)
    # X25519 public key as hex; only ever sent inside INVITE_REDEEM.
    encryption_pubkey: str = field(init=False)
    # First 8 hex chars of pubkey: the only form of it that goes into logs.
    short_id: str = field(init=False)

    def __post_init__(self) -> None:
        self.pubkey = self.signing_key.verify_key.encode().hex()
        self.encryption_pubkey = self.encryption_key.public_key.encode().hex()
        self.short_id = self.pubkey[:8]

    def __repr__(self) -> str:
        # The default dataclass repr would print the private keys into any log line.
        return f"Identity(pubkey={self.short_id}…)"

    def sign(self, message: bytes) -> str:
        """Sign `message` and return just the 64-byte signature as hex."""
        return self.signing_key.sign(message).signature.hex()

    @staticmethod
    def generate() -> "Identity":
        """Make a fresh identity in memory, without saving it."""
        return Identity(SigningKey.generate(), PrivateKey.generate())

    @staticmethod
    def create(path: Path, passphrase: str, kdf: str = config.DEFAULT_KDF) -> "Identity":
        """Generate a new identity and save it, encrypted, to `path`.

        Refuses to overwrite an existing identity: losing keys loses the account.
        """
        if path.exists():
            raise FileExistsError(f"{path} already exists; refusing to overwrite an identity")
        identity = Identity.generate()
        identity.save(path, passphrase, kdf)
        return identity

    @staticmethod
    def load(path: Path, passphrase: str) -> "Identity":
        """Decrypt and load an identity. Raises keyfile.KeyFileError on any problem."""
        plaintext = keyfile.read_keyfile(path, config.IDENTITY_TAG, passphrase)
        return _identity_from_plaintext(plaintext)

    def save(self, path: Path, passphrase: str, kdf: str = config.DEFAULT_KDF) -> None:
        """Encrypt both private keys with the passphrase and write them atomically."""
        plaintext = canonical.dumps(
            {
                "v": IDENTITY_FILE_VERSION,
                "signing_key": self.signing_key.encode().hex(),
                "encryption_key": self.encryption_key.encode().hex(),
            }
        )
        keyfile.write_keyfile(path, config.IDENTITY_TAG, plaintext, passphrase, kdf)


def verify_signature(pubkey_hex: str, message: bytes, signature_hex: str) -> bool:
    """True if `signature_hex` is pubkey's valid Ed25519 signature over `message`.

    Callers must already have checked both hex strings' format and length.
    """
    try:
        VerifyKey(bytes.fromhex(pubkey_hex)).verify(message, bytes.fromhex(signature_hex))
    except (BadSignatureError, ValueError):
        # ValueError covers bytes that aren't a valid curve point.
        return False
    return True


def _identity_from_plaintext(plaintext: bytes) -> Identity:
    # The file decrypted, so it was written by us with this passphrase; still,
    # check its shape so a format mistake gives a clear error, not a crash.
    try:
        data = canonical.loads(plaintext)
        if data.get("v") != IDENTITY_FILE_VERSION:
            raise ValueError("unknown identity file version")
        signing_seed = bytes.fromhex(data["signing_key"])
        encryption_secret = bytes.fromhex(data["encryption_key"])
        return Identity(SigningKey(signing_seed), PrivateKey(encryption_secret))
    except (ValueError, KeyError, TypeError, AttributeError) as error:
        raise keyfile.KeyFileError(
            "bad_contents", f"identity.key decrypted but its contents are invalid: {error}"
        ) from error
