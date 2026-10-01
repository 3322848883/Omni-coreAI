"""带重试的 plink 远程执行（SSH 抖动时自动重试）。"""
import subprocess
import sys
import time

PLINK = r"C:\Program Files\PuTTY\plink.exe"
HOSTKEY = "SHA256:Deubqxn2xZ1jdtGc2IgPKSYkaUrsl/p105cgCPBTvek"
PASS = "x2w6iVf1W1NWz5sB4E"
TARGET = "root@69.12.85.185"
PORT = "2222"


def run(cmd: str, retries: int = 6, timeout: int = 120) -> tuple[int, str]:
    last = ""
    for i in range(retries):
        try:
            r = subprocess.run(
                [PLINK, "-batch", "-ssh", "-hostkey", HOSTKEY, "-pw", PASS,
                 "-P", PORT, TARGET, cmd],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=timeout,
            )
            out = (r.stdout or "") + (r.stderr or "")
            if "Connection timed out" in out or "Network error" in out:
                last = out.strip()
                time.sleep(3 * (i + 1))
                continue
            return r.returncode, out
        except subprocess.TimeoutExpired:
            last = "local timeout"
            time.sleep(3 * (i + 1))
    return 1, f"FAILED after {retries} retries: {last}"


if __name__ == "__main__":
    code, out = run(sys.argv[1] if len(sys.argv) > 1 else "echo OK")
    print(out)
    sys.exit(code)
