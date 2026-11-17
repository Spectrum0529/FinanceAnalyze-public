# curl-impersonate native dependency notices

The Windows `curl_cffi` 0.17.0 dependency distributes a renamed copy of the upstream curl-impersonate 2.3.1 library. The accompanying license files are copied unchanged from its official Windows x86-64 release archive. They cover curl-impersonate and its bundled curl, BoringSSL, Brotli, c-ares, nghttp2, nghttp3, ngtcp2, zlib and zstd components; their original copyright notices and terms apply.

- [Official source and build instructions](https://github.com/lexiforest/curl-impersonate/tree/v2.3.1)
- [Pinned dependency versions and source archive hashes](https://github.com/lexiforest/curl-impersonate/blob/v2.3.1/CMakeLists.txt)
- [curl_cffi native build integration](https://github.com/lexiforest/curl_cffi/blob/v0.17.0/scripts/build.py)
- [Official Windows archive](https://github.com/lexiforest/curl-impersonate/releases/download/v2.3.1/libcurl-impersonate-v2.3.1.x86_64-win32.tar.gz)

Official archive SHA-256: `22753b8eaeb2d69fa4272e0b8282106e6d411195e6515399a21767c26750a64c`.

Native DLL SHA-256: `ae864b926b0a86bfa3b18a664407a1abb71c31a30c317d938f8c88537d044e45`.

FinanceAnalyze's MIT license does not replace these third-party terms. No changes to the upstream native library are made by this project.
