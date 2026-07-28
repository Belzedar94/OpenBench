# survive50-verify — native checker for atomic-survival-threshold-v1.
#
# THE PIN.  This tool is built against UPSTREAM Fairy-Stockfish, never against
# our Atomic-Stockfish fork.  That is the whole point: our fork solves and
# emits certificates, upstream verifies them, and a move generator bug would
# have to exist identically in both to get past.  pyffish is upstream with a
# Python skin, so the Python reference in atomicdb/survive.py and this binary
# share a lineage on purpose -- what is dropped here is the skin, not the
# second implementation.  Linking this against Atomic-Stockfish would make the
# verifier worthless.
#
#     repository  https://github.com/fairy-stockfish/Fairy-Stockfish
#     commit      fb78cb561aa01708338e35b3dc3b65a42149a3c4   (2026-07-01)
#     pyffish     setup.py at that commit declares 0.0.89, the same version
#                 as the wheel the Python reference imports
#
# Reproduce the pinned tree with:
#
#     git clone https://github.com/fairy-stockfish/Fairy-Stockfish <dir>
#     git -C <dir> checkout fb78cb561aa01708338e35b3dc3b65a42149a3c4
#     make -C <dir>/src build ARCH=x86-64-bmi2 COMP=mingw
#
# then build this with FSF_SRC pointing at <dir>/src.  Upstream's own objects
# are reused rather than recompiled, so the verifier is built with exactly the
# flags upstream builds itself with and there is no second opinion about them.

FSF_COMMIT = fb78cb561aa01708338e35b3dc3b65a42149a3c4
FSF_SRC   ?= C:/AtomicTune/fsf-pin/src

# The MSYS2 login shell that has the compiler does not necessarily have git on
# PATH, and the pin check is not optional, so the two are decoupled.
GIT      ?= git

CXX      ?= g++
CXXFLAGS ?= -Wall -Wcast-qual -fno-exceptions -std=c++17 -Wextra -Wshadow \
            -DNDEBUG -O3 -fno-strict-aliasing -DIS_64BIT \
            -msse -msse3 -mpopcnt -DUSE_POPCNT -DUSE_AVX2 -mavx2 -mbmi \
            -DUSE_SSE41 -msse4.1 -DUSE_SSSE3 -mssse3 -DUSE_SSE2 -msse2 \
            -DUSE_PEXT -mbmi2 -DNNUE_EMBEDDING_OFF
LDFLAGS  ?= -static

TARGET = survive50-verify.exe

# Every upstream object except main.o, whose main() this tool replaces.
FSF_OBJS = $(filter-out $(FSF_SRC)/main.o,$(wildcard $(FSF_SRC)/*.o))

.PHONY: all clean check-pin

all: check-pin $(TARGET)

# Refuse to build against a tree that is not the pinned commit.  A verifier
# silently built against a different move generator is worse than no verifier,
# because it still prints VERIFIED.
check-pin:
	@test -d "$(FSF_SRC)" || { echo "FSF_SRC not found: $(FSF_SRC)"; exit 1; }
	@actual=$$("$(GIT)" -C "$(FSF_SRC)" rev-parse HEAD 2>/dev/null); \
	 if [ -z "$$actual" ]; then \
	   echo "cannot read the FSF commit with GIT=$(GIT)."; \
	   echo "pass GIT=<path to git> -- the pin check is not skippable."; \
	   exit 1; \
	 fi; \
	 if [ "$$actual" != "$(FSF_COMMIT)" ]; then \
	   echo "FSF pin mismatch:"; \
	   echo "  expected $(FSF_COMMIT)"; \
	   echo "  found    $$actual"; \
	   exit 1; \
	 fi
	@test -n "$(FSF_OBJS)" || { echo "no upstream objects in $(FSF_SRC); run its make build first"; exit 1; }

$(TARGET): survive50_verify.cpp $(FSF_OBJS)
	$(CXX) $(CXXFLAGS) -I$(FSF_SRC) -o $@ survive50_verify.cpp $(FSF_OBJS) $(LDFLAGS)

clean:
	rm -f $(TARGET)
