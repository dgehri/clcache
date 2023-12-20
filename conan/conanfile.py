from conans import ConanFile # type: ignore
from conan.tools.files import rename

class ClcacheConan(ConanFile):
    name = "clcache"
    version = "4.4.26"
    author = "Daniel Gehriger <dgehriger@globusmedical.com>"
    settings = "os", "arch"
    description = "A compiler cache for Microsoft Visual Studio"
    url = "https://github.com/dgehri/clcache"
    license = "https://github.com/dgehri/clcache/blob/master/LICENSE"
    user = "dgehri"
    channel = "stable"

    def package(self):
        # Copy clcache.exe to bin and rename to moccache.exe
        self.copy("*", dst="bin/py", src="../clcache.dist")
        rename(self, f"{self.package_folder}/bin/py/clcache.exe", f"{self.package_folder}/bin/py/moccache.exe")

        # Copy clcache.exe to bin/py
        self.copy("clcache.exe", dst="bin/py", src="../clcache.dist")
        
        # Copy clcache_launcher.exe and rename to clcache.exe
        self.copy("clcache_launcher.exe", dst="bin", src="../clcache_launcher/target/release")
        rename(self, f"{self.package_folder}/bin/clcache_launcher.exe", f"{self.package_folder}/bin/clcache.exe")
        
        # Copy clcache_launcher.exe and rename to moccache.exe
        self.copy("clcache_launcher.exe", dst="bin", src="../clcache_launcher/target/release")
        rename(self, f"{self.package_folder}/bin/clcache_launcher.exe", f"{self.package_folder}/bin/moccache.exe")
        
        # Copy clcache_server.exe
        self.copy("clcache_server.exe", dst="bin", src="../clcache_server/target/release")
        
        
        self.copy("*", dst=".", src="doc")

    def package_info(self):
        self.cpp_info.libs = []

    def configure(self):  # sourcery skip: raise-specific-error
        if self.settings.os != "Windows" or self.settings.arch != "x86_64": # type: ignore
            raise Exception("This package does not support this configuration")