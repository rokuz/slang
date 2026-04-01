#!/usr/bin/env python3
"""
Build Slang for iOS, Android and WASM, then publish as a GitHub release on a fork.

Usage:
    python3 extras/build-mobile.py [--tag <version>] [--fork <owner/repo>]
                                   [--skip-ios] [--skip-ios-simulator]
                                   [--skip-android] [--skip-wasm]
                                   [--local[=PLATFORM,...]]

    --local             Build all platforms locally (no GitHub release)
    --local=wasm        Build only WASM locally
    --local=ios,android Build only iOS and Android locally

Requirements:
    - macOS with Xcode (for iOS builds)
    - Android NDK with ANDROID_NDK_HOME set (for Android builds)
    - cmake, ninja, gh (GitHub CLI, authenticated)
    - Emscripten SDK is auto-downloaded for WASM builds

The script:
    1. Fetches the latest release tag from shader-slang/slang (or uses --tag)
    2. Checks out that tag
    3. Builds host generators
    4. Cross-compiles for iOS device (arm64), iOS Simulator (arm64), Android (arm64-v8a + x86_64), and WASM (static)
    5. Packages via cmake --install into zip archives matching upstream layout
    6. Creates a GitHub release on the fork and uploads the archives
"""

import argparse
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
ROOT_DIR = SCRIPT_DIR.parent


def run(cmd: list[str], *, quiet: bool = False, check: bool = True, **kwargs) -> subprocess.CompletedProcess:
    """Run a command, optionally suppressing output on success and showing it on failure."""
    if quiet:
        result = subprocess.run(cmd, capture_output=True, text=True, **kwargs)
        if result.returncode != 0:
            if check:
                print(f"Command failed (retrying with output): {' '.join(cmd)}", flush=True)
                return subprocess.run(cmd, check=True, **kwargs)
        return result
    return subprocess.run(cmd, check=check, **kwargs)


def get_latest_tag() -> str:
    """Fetch the latest release tag from shader-slang/slang."""
    print("==> Fetching latest release tag from shader-slang/slang...")
    result = run(
        ["gh", "api", "repos/shader-slang/slang/releases/latest", "--jq", ".tag_name"],
        capture_output=True, text=True,
    )
    tag = result.stdout.strip()
    if not tag:
        sys.exit("ERROR: Could not determine latest release tag.")
    return tag


def checkout_tag(tag: str) -> str:
    """Checkout the given tag and return the previous branch name."""
    result = run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True, text=True, check=False, cwd=ROOT_DIR,
    )
    current_branch = result.stdout.strip() if result.returncode == 0 else ""

    print(f"==> Fetching tag {tag}...")
    # Try fetching the tag from all remotes until one succeeds
    fetched = False
    for remote in ("origin", "upstream"):
        result = run(
            ["git", "fetch", remote, f"refs/tags/{tag}:refs/tags/{tag}"],
            check=False, capture_output=True, text=True, cwd=ROOT_DIR,
        )
        if result.returncode == 0:
            fetched = True
            break
    if not fetched:
        sys.exit(f"ERROR: Could not fetch tag {tag} from any remote.")

    print(f"==> Checking out tag {tag}...")
    run(["git", "checkout", f"refs/tags/{tag}"], cwd=ROOT_DIR)
    print("==> Updating submodules...")
    run(["git", "submodule", "update", "--init", "--recursive"], quiet=True, cwd=ROOT_DIR)
    return current_branch


def build_generators() -> Path:
    """Build host generators needed for cross-compilation. Returns path to bin/."""
    print("==> Building host generators...")
    run(["cmake", "--workflow", "--preset", "generators", "--fresh"], quiet=True, cwd=ROOT_DIR)

    generators_prefix = ROOT_DIR / "generators"
    if generators_prefix.exists():
        shutil.rmtree(generators_prefix)
    generators_prefix.mkdir()

    run(
        ["cmake", "--install", str(ROOT_DIR / "build"),
         "--prefix", str(generators_prefix), "--component", "generators",
         "--config", "Release"],
        cwd=ROOT_DIR,
    )
    generators_bin = generators_prefix / "bin"
    print(f"==> Generators installed at: {generators_bin}")
    return generators_bin


def package_build(tag: str, platform: str, build_dir: Path, config: str, staging_dir: Path, *, static_only: bool = False, dep_libs: list[str] | None = None) -> Path:
    """Package build output matching the upstream release archive layout.

    Layout:
        LICENSE, README.md, include/, lib/, share/doc/slang/

    Args:
        dep_libs: Extra static library names (e.g. ["miniz", "lz4"]) to find
                  recursively in build_dir and include in the package.
    """
    pkg = staging_dir / f"slang-{tag}-{platform}"
    if pkg.exists():
        shutil.rmtree(pkg)
    pkg.mkdir()

    # Root files
    for f in ("LICENSE", "README.md"):
        src = ROOT_DIR / f
        if src.exists():
            shutil.copy2(src, pkg / f)

    # include/ — all public headers
    include_dst = pkg / "include"
    shutil.copytree(ROOT_DIR / "include", include_dst, dirs_exist_ok=True)

    # Also copy the generated slang-tag-version.h if present
    for gen_header in build_dir.rglob("slang-tag-version.h"):
        shutil.copy2(gen_header, include_dst / gen_header.name)
        break

    # lib/ — static and shared libraries from the build
    lib_dst = pkg / "lib"
    lib_dst.mkdir()
    lib_extensions = {".a"} if static_only else {".a", ".so", ".dylib"}
    # Search common multi-config output layouts
    for search_dir in (
        build_dir / config / "lib",
        build_dir / "lib" / config,
        build_dir / "lib",
    ):
        if not search_dir.is_dir():
            continue
        for entry in search_dir.iterdir():
            dst = lib_dst / entry.name
            if dst.exists():
                continue
            if entry.is_file() and entry.suffix in lib_extensions:
                shutil.copy2(entry, dst)

    # Collect transitive dependency static libs from the build tree.
    # These are built under external/ and not installed to lib/ by default.
    if dep_libs:
        for lib_name in dep_libs:
            # Search for libNAME.a anywhere in the build tree
            for candidate in build_dir.rglob(f"lib{lib_name}.a"):
                dst = lib_dst / candidate.name
                if not dst.exists():
                    shutil.copy2(candidate, dst)
                    break

    # share/doc/slang/ — documentation
    docs_src = ROOT_DIR / "docs"
    if docs_src.is_dir():
        docs_dst = pkg / "share" / "doc" / "slang"
        shutil.copytree(docs_src, docs_dst, dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns(".*"))

    # Create zip
    zip_path = staging_dir / f"slang-{tag}-{platform}.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for file in sorted(pkg.rglob("*")):
            if file.is_file():
                zf.write(file, file.relative_to(pkg))
    return zip_path


# Common CMake flags to disable features irrelevant to mobile cross-compilation.
_MOBILE_CMAKE_FLAGS = [
    "-DSLANG_SLANG_LLVM_FLAVOR=DISABLE",
    "-DSLANG_ENABLE_AFTERMATH=OFF",
    "-DSLANG_ENABLE_CUDA=OFF",
    "-DSLANG_ENABLE_GFX=OFF",
    "-DSLANG_ENABLE_OPTIX=OFF",
    "-DSLANG_ENABLE_REPLAYER=OFF",
    "-DSLANG_ENABLE_SLANG_RHI=OFF",
    "-DSLANG_ENABLE_SPLIT_DEBUG_INFO=OFF",
    "-DSLANG_ENABLE_TESTS=OFF",
    "-DSLANG_ENABLE_SLANGD=OFF",
    "-DSLANG_ENABLE_EXAMPLES=OFF",
    "-DSLANG_ENABLE_XLIB=OFF",
    "-DSLANG_ENABLE_NVAPI=OFF",
]


def build_ios(tag: str, generators_bin: Path, staging_dir: Path) -> Path:
    """Cross-compile Slang for iOS arm64 and return the zip path."""
    print()
    print("========================================")
    print("==> Building for iOS (arm64, MSL-only)")
    print("========================================")

    build_dir = ROOT_DIR / "build-ios-arm64"
    if build_dir.exists():
        shutil.rmtree(build_dir)

    cmake_args = [
        "cmake", "-S", str(ROOT_DIR), "-B", str(build_dir),
        "-G", "Ninja Multi-Config",
        "-DCMAKE_SYSTEM_NAME=iOS",
        "-DCMAKE_OSX_ARCHITECTURES=arm64",
        "-DCMAKE_OSX_DEPLOYMENT_TARGET=15.0",
        "-DCMAKE_MACOSX_BUNDLE=OFF",
        f"-DSLANG_GENERATORS_PATH={generators_bin}",
        "-DSLANG_LIB_TYPE=STATIC",
        "-DSLANG_ENABLE_SLANGC=OFF",
        "-DSLANG_ENABLE_SLANGI=OFF",
        "-DSLANG_ENABLE_SLANG_GLSLANG=OFF",
    ] + _MOBILE_CMAKE_FLAGS
    run(cmake_args, quiet=True, cwd=ROOT_DIR)

    print("==> Building iOS Release...")
    run(["cmake", "--build", str(build_dir), "--config", "Release"], quiet=True, cwd=ROOT_DIR)

    zip_path = package_build(
        tag, "ios-arm64", build_dir, "Release", staging_dir,
        static_only=True, dep_libs=["miniz", "lz4_static", "cmark-gfm"],
    )
    print(f"==> iOS package: {zip_path}")
    return zip_path


def build_ios_simulator(tag: str, generators_bin: Path, staging_dir: Path) -> Path:
    """Cross-compile Slang for iOS Simulator (arm64 + x86_64) and return the zip path."""
    print()
    print("========================================")
    print("==> Building for iOS Simulator (arm64, MSL-only)")
    print("========================================")

    build_dir = ROOT_DIR / "build-ios-simulator"
    if build_dir.exists():
        shutil.rmtree(build_dir)

    cmake_args = [
        "cmake", "-S", str(ROOT_DIR), "-B", str(build_dir),
        "-G", "Ninja Multi-Config",
        "-DCMAKE_SYSTEM_NAME=iOS",
        "-DCMAKE_OSX_ARCHITECTURES=arm64",
        "-DCMAKE_OSX_DEPLOYMENT_TARGET=15.0",
        "-DCMAKE_OSX_SYSROOT=iphonesimulator",
        "-DCMAKE_MACOSX_BUNDLE=OFF",
        f"-DSLANG_GENERATORS_PATH={generators_bin}",
        "-DSLANG_LIB_TYPE=STATIC",
        "-DSLANG_ENABLE_SLANGC=OFF",
        "-DSLANG_ENABLE_SLANGI=OFF",
        "-DSLANG_ENABLE_SLANG_GLSLANG=OFF",
    ] + _MOBILE_CMAKE_FLAGS
    run(cmake_args, quiet=True, cwd=ROOT_DIR)

    print("==> Building iOS Simulator Release...")
    run(["cmake", "--build", str(build_dir), "--config", "Release"], quiet=True, cwd=ROOT_DIR)

    zip_path = package_build(
        tag, "iossimulator-arm64", build_dir, "Release", staging_dir,
        static_only=True, dep_libs=["miniz", "lz4_static", "cmark-gfm"],
    )
    print(f"==> iOS Simulator package: {zip_path}")
    return zip_path


def ensure_emsdk() -> Path:
    """Ensure the Emscripten SDK is installed and activated. Returns emsdk root."""
    emsdk_dir = ROOT_DIR / "emsdk"
    emsdk_script = emsdk_dir / "emsdk"

    if not emsdk_script.exists():
        print("==> Downloading Emscripten SDK...")
        run(
            ["git", "clone", "https://github.com/emscripten-core/emsdk.git", str(emsdk_dir)],
            quiet=True,
        )

    print("==> Installing latest Emscripten toolchain...")
    run([str(emsdk_script), "install", "latest"], quiet=True, cwd=emsdk_dir)
    print("==> Activating Emscripten toolchain...")
    run([str(emsdk_script), "activate", "latest"], quiet=True, cwd=emsdk_dir)

    # Locate emcmake — it lives in emsdk_dir or emsdk_dir/upstream/emscripten
    emcmake = shutil.which("emcmake")
    if emcmake is None:
        upstream = emsdk_dir / "upstream" / "emscripten"
        if (upstream / "emcmake").exists():
            os.environ["PATH"] = str(upstream) + os.pathsep + os.environ.get("PATH", "")
        # Also source emsdk_env to set EM_CONFIG etc.
        env_script = emsdk_dir / "emsdk_env.sh"
        if env_script.exists():
            result = run(
                ["bash", "-c", f"source {env_script} && env"],
                capture_output=True, text=True, cwd=emsdk_dir,
            )
            for line in result.stdout.splitlines():
                if "=" in line:
                    key, _, val = line.partition("=")
                    if key in ("PATH", "EMSDK", "EM_CONFIG", "EMSDK_NODE"):
                        os.environ[key] = val

    emcmake = shutil.which("emcmake")
    if emcmake is None:
        sys.exit("ERROR: emcmake not found after emsdk install. Check emsdk setup.")
    print(f"==> emcmake found: {emcmake}")
    return emsdk_dir


def build_wasm_static(tag: str, generators_bin: Path, staging_dir: Path) -> Path:
    """Build Slang as static libraries for Emscripten/WASM and return the zip path."""
    print()
    print("========================================")
    print("==> Building for WASM (static libraries)")
    print("========================================")

    ensure_emsdk()

    build_dir = ROOT_DIR / "build-wasm-static"
    if build_dir.exists():
        shutil.rmtree(build_dir)

    cmake_args = [
        "emcmake", "cmake", "-S", str(ROOT_DIR), "-B", str(build_dir),
        "-G", "Ninja Multi-Config",
        f"-DSLANG_GENERATORS_PATH={generators_bin}",
        "-DSLANG_LIB_TYPE=STATIC",
        "-DCMAKE_C_FLAGS_INIT=-fwasm-exceptions -Os",
        "-DCMAKE_CXX_FLAGS_INIT=-fwasm-exceptions -Os",
        "-DSLANG_ENABLE_SLANGC=OFF",
        "-DSLANG_ENABLE_SLANGI=OFF",
        "-DSLANG_ENABLE_SLANG_GLSLANG=OFF",
    ] + _MOBILE_CMAKE_FLAGS
    run(cmake_args, quiet=True, cwd=ROOT_DIR)

    print("==> Building WASM Release...")
    run(["cmake", "--build", str(build_dir), "--config", "Release"], quiet=True, cwd=ROOT_DIR)

    zip_path = package_build(
        tag, "wasm-static", build_dir, "Release", staging_dir,
        static_only=True, dep_libs=["miniz", "lz4_static", "cmark-gfm"],
    )
    print(f"==> WASM static package: {zip_path}")
    return zip_path


def build_android(tag: str, generators_bin: Path, staging_dir: Path) -> list[Path]:
    """Cross-compile Slang for Android architectures and return zip paths."""
    ndk_home = os.environ.get("ANDROID_NDK_HOME", "")
    if not ndk_home:
        print("ERROR: ANDROID_NDK_HOME is not set. Skipping Android builds.")
        print("       Set it to your NDK installation path, e.g.:")
        print("       export ANDROID_NDK_HOME=$HOME/Library/Android/sdk/ndk/<version>")
        return []

    archs = [
        ("arm64-v8a", "android-arm64", "build-android-arm64-v8a"),
        ("x86_64", "android-x86_64", "build-android-x86_64"),
    ]
    zips = []

    for abi, preset, build_dir_name in archs:
        print()
        print("========================================")
        print(f"==> Building for Android {abi} (SPIR-V-only)")
        print("========================================")

        build_dir = ROOT_DIR / build_dir_name
        if build_dir.exists():
            shutil.rmtree(build_dir)

        run(
            ["cmake", "--preset", preset, "--fresh",
             f"-DSLANG_GENERATORS_PATH={generators_bin}"],
            quiet=True, cwd=ROOT_DIR,
        )

        print(f"==> Building Android {abi} Release...")
        run(
            ["cmake", "--build", "--preset", f"{preset}-release"],
            quiet=True, cwd=ROOT_DIR,
        )

        zip_path = package_build(tag, f"android-{abi}", build_dir, "Release", staging_dir)
        print(f"==> Android {abi} package: {zip_path}")
        zips.append(zip_path)

    return zips


def create_release(tag: str, fork_repo: str, artifacts: list[Path]):
    """Create a GitHub release on the fork and upload artifacts."""
    release_tag = f"{tag}-mobile"
    release_title = f"Slang {tag} - Mobile (iOS + Android)"

    print()
    print("========================================")
    print(f"==> Creating GitHub release on {fork_repo}")
    print(f"==> Tag: {release_tag}")
    print("========================================")

    # Build release notes
    rows = []
    for artifact in artifacts:
        name = artifact.stem
        if "iossimulator" in name:
            rows.append("| iOS Simulator | arm64 | MSL (Metal) |")
        elif "ios" in name:
            rows.append("| iOS | arm64 | MSL (Metal) |")
        elif "arm64" in name:
            rows.append("| Android | arm64-v8a | SPIR-V (Vulkan) |")
        elif "x86_64" in name:
            rows.append("| Android | x86_64 | SPIR-V (Vulkan) |")
        elif "wasm" in name:
            rows.append("| WASM | wasm32 | WGSL |")

    table_rows = "\n".join(rows)
    notes = f"""\
## Slang {tag} Mobile Libraries

Built from upstream [shader-slang/slang {tag}](https://github.com/shader-slang/slang/releases/tag/{tag}).

### Packages

| Platform | Architecture | Target Backend |
|----------|-------------|----------------|
{table_rows}

### Contents
Each archive follows the same layout as upstream releases:
- `LICENSE`, `README.md`
- `include/` — public headers
- `lib/` — libraries (iOS/WASM: static `.a`, Android: shared `.so`)
- `share/doc/slang/` — documentation

### Usage
**iOS / iOS Simulator / WASM**: Link against the static libraries (`.a`).
Transitive dependencies (`libminiz.a`, `liblz4_static.a`, `libcmark-gfm.a`)
are included in the archive. WASM libraries are built with `-fwasm-exceptions -Os`.

**Android**: Link against the shared libraries (`.so`). All dependencies are
bundled inside the shared libraries.

The Slang compiler API can generate MSL (iOS) or SPIR-V (Android) from Slang shaders at runtime."""

    # Delete existing release if it exists (to allow re-runs)
    run(["gh", "release", "delete", release_tag, "--repo", fork_repo, "--yes"],
        check=False, capture_output=True)
    run(["gh", "api", f"repos/{fork_repo}/git/refs/tags/{release_tag}", "-X", "DELETE"],
        check=False, capture_output=True)

    # Get current commit SHA for the release target
    result = run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, cwd=ROOT_DIR)
    commit_sha = result.stdout.strip()

    # Create the release with artifacts
    cmd = [
        "gh", "release", "create", release_tag,
        "--repo", fork_repo,
        "--title", release_title,
        "--notes", notes,
        "--target", commit_sha,
    ] + [str(a) for a in artifacts]
    run(cmd)

    print()
    print(f"==> Release published: https://github.com/{fork_repo}/releases/tag/{release_tag}")


def main():
    parser = argparse.ArgumentParser(description="Build Slang for iOS and Android, publish as GitHub release.")
    parser.add_argument("--tag", default="", help="Release tag to build (default: latest from shader-slang/slang)")
    parser.add_argument("--fork", default="rokuz/slang", help="GitHub repo to publish release to (default: rokuz/slang)")
    _ALL_PLATFORMS = ("ios", "ios-simulator", "android", "wasm")
    parser.add_argument("--skip-ios", action="store_true", help="Skip iOS device build")
    parser.add_argument("--skip-ios-simulator", action="store_true", help="Skip iOS Simulator build")
    parser.add_argument("--skip-android", action="store_true", help="Skip Android build")
    parser.add_argument("--skip-wasm", action="store_true", help="Skip WASM static build")
    parser.add_argument(
        "--local", nargs="?", const="all", default=None, metavar="PLATFORM",
        help="Local build only — produce zips without publishing to GitHub. "
             f"Values: all (default), or comma-separated list of: {', '.join(_ALL_PLATFORMS)}",
    )
    args = parser.parse_args()

    # Determine which platforms to build
    if args.local is not None:
        local_platforms = set(_ALL_PLATFORMS) if args.local == "all" else set(args.local.split(","))
        unknown = local_platforms - set(_ALL_PLATFORMS)
        if unknown:
            sys.exit(f"ERROR: Unknown platform(s): {', '.join(sorted(unknown))}. "
                     f"Valid: {', '.join(_ALL_PLATFORMS)}")
        args.skip_ios = "ios" not in local_platforms
        args.skip_ios_simulator = "ios-simulator" not in local_platforms
        args.skip_android = "android" not in local_platforms
        args.skip_wasm = "wasm" not in local_platforms

    tag = args.tag or get_latest_tag()
    print(f"==> Using release tag: {tag}")

    current_branch = checkout_tag(tag)

    staging_dir = ROOT_DIR / "build-mobile-staging"
    if staging_dir.exists():
        shutil.rmtree(staging_dir)
    staging_dir.mkdir()

    generators_bin = build_generators()

    artifacts: list[Path] = []

    if not args.skip_ios:
        artifacts.append(build_ios(tag, generators_bin, staging_dir))

    if not args.skip_ios_simulator:
        artifacts.append(build_ios_simulator(tag, generators_bin, staging_dir))

    if not args.skip_android:
        artifacts.extend(build_android(tag, generators_bin, staging_dir))

    if not args.skip_wasm:
        artifacts.append(build_wasm_static(tag, generators_bin, staging_dir))

    if not artifacts:
        sys.exit("ERROR: No artifacts were built. Nothing to release.")

    print()
    print("==> Artifacts:")
    for a in artifacts:
        print(f"    {a}")

    if args.local is not None:
        print()
        print("==> Local mode: skipping GitHub release.")
    else:
        create_release(tag, args.fork, artifacts)

    # Return to original branch
    if current_branch and current_branch != "HEAD":
        print(f"==> Returning to branch: {current_branch}")
        run(["git", "checkout", current_branch], cwd=ROOT_DIR)

    print("==> Done!")


if __name__ == "__main__":
    main()
