"""SHA-256 pins for the llama.cpp release assets VARIANT-1 installs.

Values are GitHub's published asset digests for the pinned tag. Every download
is checked against its pin before extraction; an unpinned asset is refused.
"""

from __future__ import annotations

LLAMA_TAG = "b10679"

# asset name -> (sha256, size in bytes)
LLAMA_ASSET_PINS: dict[str, tuple[str, int]] = {
    "llama-b10679-bin-win-cpu-x64.zip": (
        "c0dec4dfb52919e17f0a108a94bfbe877c67d77825145079e7703fc84f63986e",
        18130542,
    ),
    "llama-b10679-bin-win-cpu-arm64.zip": (
        "d67ca67f9bd1d9dfc49819bdd9453973ef2242d10f1a47b2a46ea74a67fc5598",
        11897914,
    ),
    "llama-b10679-bin-win-vulkan-x64.zip": (
        "d288a375a324f650a587d3b876afe692ca3586110f20b863fadbe91dd3b93469",
        34914877,
    ),
    "llama-b10679-bin-win-cuda-13.3-x64.zip": (
        "2936f7230732df0dda2070960a940a7ca69d4debbd5edff4a1b98c2dad339efb",
        146519312,
    ),
    "cudart-llama-bin-win-cuda-13.3-x64.zip": (
        "1462a050eb4c684921ba51dcc4cc488a036674c3e73e9945ee705b854808d03e",
        390970417,
    ),
    "llama-b10679-bin-win-cuda-13.4-arm64.zip": (
        "69a09b194bb13d682f2a2a4f4caf8fb38bf4db7fcf9612f3ff23ff9cdd7ce7a2",
        140090029,
    ),
    "cudart-llama-bin-win-cuda-13.4-arm64.zip": (
        "5a40dc7c5fa3d0a80ceeba4f16f9e8d25d87bcf1399c9233588953c43436c33c",
        153318797,
    ),
    "llama-b10679-bin-ubuntu-x64.tar.gz": (
        "d39bbf43130c3810351de1e93ce2019924924844f48065a69f9053bb81f2eded",
        16383819,
    ),
    "llama-b10679-bin-ubuntu-arm64.tar.gz": (
        "dde5217b6c646c8422c576e7ccafdb0f51ba4f2f80685379e987ed5115df33b0",
        13126811,
    ),
    "llama-b10679-bin-ubuntu-vulkan-x64.tar.gz": (
        "57558044334fb6b09cd33a4bfcbc5fd99bb544366236d9dd77df76cf32fe27c2",
        33452068,
    ),
    "llama-b10679-bin-ubuntu-vulkan-arm64.tar.gz": (
        "b4321f46e1d41d207cc55f3876cd475d20f9ed904204f785c3421d8e8485839a",
        27274846,
    ),
    "llama-b10679-bin-macos-arm64.tar.gz": (
        "e59761b1cbf0669e5b06eea594e563aa8df329967e408d6deb7f83b30edc7150",
        11026756,
    ),
    "llama-b10679-bin-macos-x64.tar.gz": (
        "8098c025dc6dfa4199a1234f5cda3bd5a58429134778065d8c302ecef8c3c419",
        11091103,
    ),
}

__all__ = ["LLAMA_ASSET_PINS", "LLAMA_TAG"]
