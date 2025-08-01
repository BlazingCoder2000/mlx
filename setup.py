# Copyright © 2023 Apple Inc.

import datetime
import os
import platform
import re
import subprocess
from pathlib import Path
from subprocess import run

from setuptools import Command, Extension, find_namespace_packages, setup
from setuptools.command.build_ext import build_ext


def get_version():
    with open("mlx/version.h", "r") as fid:
        for l in fid:
            if "#define MLX_VERSION_MAJOR" in l:
                major = l.split()[-1]
            if "#define MLX_VERSION_MINOR" in l:
                minor = l.split()[-1]
            if "#define MLX_VERSION_PATCH" in l:
                patch = l.split()[-1]
    version = f"{major}.{minor}.{patch}"
    if "PYPI_RELEASE" not in os.environ:
        today = datetime.date.today()
        version = f"{version}.dev{today.year}{today.month:02d}{today.day:02d}"

        if "DEV_RELEASE" not in os.environ:
            git_hash = (
                run(
                    "git rev-parse --short HEAD".split(),
                    capture_output=True,
                    check=True,
                )
                .stdout.strip()
                .decode()
            )
            version = f"{version}+{git_hash}"

    return version


# A CMakeExtension needs a sourcedir instead of a file list.
# The name must be the _single_ output extension from the CMake build.
# If you need multiple extensions, see scikit-build.
class CMakeExtension(Extension):
    def __init__(self, name: str, sourcedir: str = "") -> None:
        super().__init__(name, sources=[])
        self.sourcedir = os.fspath(Path(sourcedir).resolve())


class CMakeBuild(build_ext):
    def build_extension(self, ext: CMakeExtension) -> None:
        # Must be in this form due to bug in .resolve() only fixed in Python 3.10+
        ext_fullpath = Path.cwd() / self.get_ext_fullpath(ext.name)  # type: ignore[no-untyped-call]
        extdir = ext_fullpath.parent.resolve()

        debug = int(os.environ.get("DEBUG", 0)) if self.debug is None else self.debug
        cfg = "Debug" if debug else "Release"

        # Set Python_EXECUTABLE instead if you use PYBIND11_FINDPYTHON
        # EXAMPLE_VERSION_INFO shows you how to pass a value into the C++ code
        # from Python.
        cmake_args = [
            f"-DCMAKE_INSTALL_PREFIX={extdir}{os.sep}",
            f"-DCMAKE_BUILD_TYPE={cfg}",
            "-DMLX_BUILD_PYTHON_BINDINGS=ON",
            "-DMLX_BUILD_TESTS=OFF",
            "-DMLX_BUILD_BENCHMARKS=OFF",
            "-DMLX_BUILD_EXAMPLES=OFF",
            f"-DMLX_PYTHON_BINDINGS_OUTPUT_DIRECTORY={extdir}{os.sep}",
        ]
        # Some generators require explcitly passing config when building.
        build_args = ["--config", cfg]
        # Adding CMake arguments set as environment variable
        # (needed e.g. to build for ARM OSx on conda-forge)
        if "CMAKE_ARGS" in os.environ:
            cmake_args += [item for item in os.environ["CMAKE_ARGS"].split(" ") if item]

        # Pass version to C++
        cmake_args += [f"-DMLX_VERSION={self.distribution.get_version()}"]  # type: ignore[attr-defined]

        if platform.system() == "Darwin":
            # Cross-compile support for macOS - respect ARCHFLAGS if set
            archs = re.findall(r"-arch (\S+)", os.environ.get("ARCHFLAGS", ""))
            if archs:
                cmake_args += ["-DCMAKE_OSX_ARCHITECTURES={}".format(";".join(archs))]
        if platform.system() == "Windows":
            # On Windows DLLs must be put in the same dir with the extension
            # while cmake puts mlx.dll into the "bin" sub-dir. Link with mlx
            # statically to work around it.
            cmake_args += ["-DBUILD_SHARED_LIBS=OFF"]
        else:
            cmake_args += ["-DBUILD_SHARED_LIBS=ON"]

        # Set CMAKE_BUILD_PARALLEL_LEVEL to control the parallel build level
        # across all generators.
        if "CMAKE_BUILD_PARALLEL_LEVEL" not in os.environ:
            # self.parallel is a Python 3 only way to set parallel jobs by hand
            # using -j in the build_ext call, not supported by pip or PyPA-build.
            if hasattr(self, "parallel") and self.parallel:
                # CMake 3.12+ only.
                build_args += [f"-j{self.parallel}"]

        build_temp = Path(self.build_temp) / ext.name
        if not build_temp.exists():
            build_temp.mkdir(parents=True)

        subprocess.run(
            ["cmake", ext.sourcedir, *cmake_args], cwd=build_temp, check=True
        )
        subprocess.run(
            ["cmake", "--build", ".", "--target", "install", *build_args],
            cwd=build_temp,
            check=True,
        )

    # Make sure to copy mlx.metallib for inplace builds
    def run(self):
        super().run()

        # Based on https://github.com/pypa/setuptools/blob/main/setuptools/command/build_ext.py#L102
        if self.inplace:
            for ext in self.extensions:
                if ext.name == "mlx.core":
                    # Resolve inplace package dir
                    build_py = self.get_finalized_command("build_py")
                    inplace_file, regular_file = self._get_inplace_equivalent(
                        build_py, ext
                    )

                    inplace_dir = str(Path(inplace_file).parent.resolve())
                    regular_dir = str(Path(regular_file).parent.resolve())

                    self.copy_tree(regular_dir, inplace_dir)


class GenerateStubs(Command):
    user_options = []

    def initialize_options(self):
        pass

    def finalize_options(self):
        pass

    def run(self) -> None:
        out_path = "python/mlx/core"
        stub_cmd = [
            "python",
            "-m",
            "nanobind.stubgen",
            "-m",
            "mlx.core",
            "-p",
            "python/mlx/_stub_patterns.txt",
        ]
        subprocess.run(stub_cmd + ["-r", "-O", out_path])
        # Run again without recursive to specify output file name
        subprocess.run(["rm", f"{out_path}/mlx.pyi"])
        subprocess.run(stub_cmd + ["-o", f"{out_path}/__init__.pyi"])


# Read the content of README.md
with open(Path(__file__).parent / "README.md", encoding="utf-8") as f:
    long_description = f.read()

# The information here can also be placed in setup.cfg - better separation of
# logic and declaration, and simpler if you include description/version in a file.
if __name__ == "__main__":
    packages = find_namespace_packages(
        where="python", exclude=["src", "tests", "tests.*"]
    )
    package_dir = {"": "python"}
    package_data = {"mlx": ["lib/*", "include/*", "share/*"], "mlx.core": ["*.pyi"]}
    install_requires = []
    build_cuda = "MLX_BUILD_CUDA=ON" in os.environ.get("CMAKE_ARGS", "")
    if build_cuda:
        install_requires = ["nvidia-cublas-cu12", "nvidia-cuda-nvrtc-cu12"]

    setup(
        name="mlx-cuda" if build_cuda else "mlx",
        version=get_version(),
        author="MLX Contributors",
        author_email="mlx@group.apple.com",
        description="A framework for machine learning on Apple silicon.",
        long_description=long_description,
        long_description_content_type="text/markdown",
        license="MIT",
        url="https://github.com/ml-explore/mlx",
        packages=packages,
        package_dir=package_dir,
        package_data=package_data,
        include_package_data=True,
        install_requires=install_requires,
        extras_require={
            "dev": [
                "nanobind==2.4.0",
                "numpy",
                "pre-commit",
                "setuptools>=42",
                "torch",
                "typing_extensions",
            ],
        },
        entry_points={
            "console_scripts": [
                "mlx.launch = mlx.distributed_run:main",
                "mlx.distributed_config = mlx.distributed_run:distributed_config",
            ]
        },
        ext_modules=[CMakeExtension("mlx.core")],
        cmdclass={"build_ext": CMakeBuild, "generate_stubs": GenerateStubs},
        zip_safe=False,
        python_requires=">=3.9",
    )
