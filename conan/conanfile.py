from conans import ConanFile
from conan.tools.files import rename, rmdir

class ClcacheConan(ConanFile):
    name = "clcache"
    version = "4.4.3ak"
    author = "Daniel Gehriger <dgehriger@globusmedical.com>"
    settings = "os", "arch"
    description = "A compiler cache for Microsoft Visual Studio"
    url = "https://github.com/dgehri/clcache"
    license = "https://github.com/dgehri/clcache/blob/master/LICENSE"
    user = "dgehri"
    channel = "dev"

    def package(self):
        self.copy("*", dst="bin", src="../clcache.dist")
        self.copy("clcache.exe", dst="tmp", src="../clcache.dist")
        self.copy("*", dst="bin", src="../bin")
        rename(self, f"{self.package_folder}/tmp/clcache.exe", f"{self.package_folder}/bin/moccache.exe")
        rmdir(self, f"{self.package_folder}/tmp")
        self.copy("*", dst=".", src="doc")

    def package_info(self):
        self.cpp_info.libs = []

    def configure(self):
        if self.settings.os != "Windows" or self.settings.arch != "x86_64":
            raise Exception("This package does not support this configuration")