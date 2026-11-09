# CairnMesh device firmware for the STM32U083CCT6 (NUCLEO-U083RC), TinyGo.
# Adapted from the stm32u083-demo project; see README.md.
#
#   make test                 host tests: wire and device behaviour vectors (no board)
#   make check-env            the toolchain: TinyGo version, commit and U083 patches (patches/tinygo.lock)
#   make size CMD=node        bench: flash / RAM of a U083 program, and the flash-end check
#   make dist                 the deployment image of cmd/wle5node with checksums (build/dist)
#   make allocs CMD=node      heap allocations of a program (keep the hot path at none)
#   make backup               save the chip to backups/board-<time>.bin (never overwrites)
#   make selftest             flash cmd/selftest, capture its report
#   make flash CMD=node       flash a program
#   make monitor              show the serial output (Ctrl-C stops it)

TARGET         ?= stm32u083
TINYGOROOT     ?= $(HOME)/src/tinygo
TINYGO         ?= tinygo
CUBEPROG_BIN   ?= $(HOME)/apps/STM32CubeProrammer/bin
STM32_PROG     ?= $(CUBEPROG_BIN)/STM32_Programmer_CLI
SERIAL_PORT    ?= /dev/ttyACM0
BAUD           ?= 115200
STACK_SIZE     ?= 4KB
# Production default. The U083 bench remains available with CMD=node.
CMD            ?= wle5node
BUILD_DIR      ?= build
RESULTS        ?= results
BACKUP         ?= backups/board-$(shell date +%Y%m%d-%H%M%S).bin
FACTORY        ?= backups/factory-backup.bin
FLASH_ADDR     ?= 0x08000000
FLASH_SIZE     ?= 0x40000
# The journals (pages 101-124, board.RecordsPage, board.MarksPage), the key record (page 125,
# board.KeyPage) and the FCNT counter (pages 126-127): the image must end below.
FLASH_LIMIT    ?= 0x32800
MONITOR_SECS   ?= 0
CONFIRM        ?= no
LDFLAGS        ?=
# BENCH=1: a bench build of cmd/node, which joins the network without read
# protection (the bench board stays at RDP level 0). Never for deployment.
BENCH          ?= 0
ifeq ($(BENCH),1)
LDFLAGS        += -X main.benchBuild=1
endif

# cmd/gateway testbed fault injection. Command line only: environment leakage
# must not silently put fault-injection code into a production build.
#   make flash CMD=gateway GATEWAY_BENCH_FAULT=radio1
#   make flash CMD=gateway GATEWAY_BENCH_FAULT=hang
ifeq ($(origin GATEWAY_BENCH_FAULT),environment)
$(error GATEWAY_BENCH_FAULT comes from the environment: give it on the command line, make build CMD=gateway GATEWAY_BENCH_FAULT=radio1)
endif
GATEWAY_BENCH_FAULT ?=
GATEWAY_TAGS        := $(if $(GATEWAY_BENCH_FAULT),cairn_testbed)
GATEWAY_LDX         := $(if $(GATEWAY_BENCH_FAULT),-X main.gatewayBenchFault=$(GATEWAY_BENCH_FAULT))
ALL_LDFLAGS         := $(strip $(LDFLAGS) $(GATEWAY_LDX))
CMD_OUT             := $(CMD)$(if $(GATEWAY_BENCH_FAULT),-testbed)
ifneq ($(and $(GATEWAY_BENCH_FAULT),$(filter wle5node,$(CMD))),)
$(error GATEWAY_BENCH_FAULT is only for CMD=gateway, not CMD=wle5node)
endif

TARGET_JSON    := $(TINYGOROOT)/targets/$(TARGET).json
TGFLAGS        := -target=$(TARGET) -stack-size=$(STACK_SIZE) $(if $(GATEWAY_TAGS),-tags="$(GATEWAY_TAGS)") $(if $(ALL_LDFLAGS),-ldflags="$(ALL_LDFLAGS)")

export TINYGOROOT

.DEFAULT_GOAL := help
.PHONY: wle5-flash-check wle5-gdb wle5-check-env tinygo-wle5-apply wle5-build wle5-size wle5-allocs wle5-assert-prod gateway-assert-prod assert-prod wle5-backup wle5-flash help check-env tinygo-apply test vectors build dist size allocs flash selftest monitor info backup restore restore-factory erase clean

help:
	@sed -n '3,13p' Makefile | sed 's/^# \{0,1\}//'
	@echo
	@echo "  TINYGOROOT ($(TINYGOROOT))  SERIAL_PORT ($(SERIAL_PORT))  CMD ($(CMD))"

check-env:
	@command -v $(TINYGO) >/dev/null 2>&1 || { echo "error: $(TINYGO) is not in PATH"; exit 1; }
	@test -f "$(TARGET_JSON)" || { echo "error: $(TARGET_JSON) missing (U083 support lives in ~/src/tinygo)"; exit 1; }
	@test -e "$(TINYGOROOT)/lib/clang" || { echo "error: $(TINYGOROOT)/lib/clang missing: see the stm32u083-demo Makefile 'setup'"; exit 1; }
	@test -x "$(STM32_PROG)" || { echo "error: STM32_Programmer_CLI not found: $(STM32_PROG)"; exit 1; }
	@sh tools/tinygo.sh check "$(TINYGOROOT)" "$(TINYGO)"

# A fresh TinyGo clone at the locked commit gets the U083 patches; refuses any other tree.
tinygo-apply:
	sh tools/tinygo.sh apply "$(TINYGOROOT)"

# STM32WLE5CCU6 (E77-400M22S module, BOARD_WLE5.md): its own TinyGo tree at the
# commit of patches/tinygo-wle5.lock, so ~/src/tinygo (U083) stays as it is.
#   make tinygo-wle5-apply        a clean clone at that commit gets the WLE5 files
#   make wle5-build CMD=wle5check [WLE5_TCXO_MV=<mV>] [WLE5_BOARD=pcb4]   (wle5check: 1800 mV when not given)
#   make wle5-build CMD=wle5cw WLE5_ANT_OK=1 WLE5_TCXO_MV=<mV> [WLE5_CW_DBM=<dBm, -9..10, default 10>]   a 10 s carrier at 433.5 MHz; antenna or 50 ohm load on! (no WLE5_ANT_OK=1: it does not transmit)
#   make wle5-build CMD=wle5link WLE5_ANT_OK=1 [WLE5_TCXO_MV=<mV>] [WLE5_LINK_DBM=<dBm, -9..10, default 10>] [WLE5_LINK_SEC=<1..3600, default 5>]   two-way link test with the U083 bench board; TRANSMITS: antenna or 50 ohm load on!
#   make wle5-build CMD=wle5node [WLE5_ANT_OK=1] [WLE5_TX_DBM=<dBm, -9..10, default 0>] [WLE5_TCXO_MV=<mV>] [BENCH=1] [WLE5_BOARD=pcb4] [WLE5_BENCH_ACTIONS=1]   the node (cmd/wle5node): multi-hop protocol (relay, HELLO, JOIN) + hardware AES/RNG/flash + USART2 console. TRANSMITS only with WLE5_ANT_OK=1 (antenna or 50 ohm load on!), at WLE5_TX_DBM (default 0 dBm; was a fixed 10 before 2026-11-07); BENCH=1 lets it join at RDP level 0 (bench only); WLE5_BENCH_ACTIONS=1 adds actions 0x02-0x05 to a testbed image. wle5-size CMD=wle5node also checks the image against FLASH_LIMIT
#   WLE5_OPT=1 builds with -opt=1 for gdb (DWARF is on by default; TinyGo strips only with -no-debug)
#   make wle5-flash-check [CMD=wle5check] [WLE5_CHECK_ARGS="-n 5 -e"]   build, flash, read the serial port, assert
#                                 (uarthello: hello lines + echo; wle5check: "wle5check PASS" with every check PASS)
#   make wle5-gdb [WLE5_GDB_ARGS="smoke"]               OpenOCD gdb server (or the gdb smoke test)
TINYGOROOT_WLE5 ?= $(HOME)/src/tinygo-wle5
WLE5_TCXO_MV    ?=
WLE5_BOARD      ?= e77
WLE5_OPT        ?=
# wle5cw only: output power in dBm (default 10 in the program; the program refuses above 10
# at start, and wle5-build refuses it here too). Ignored for any other CMD.
WLE5_CW_DBM     ?=
# wle5link only: transmit power in dBm (same ceiling) and seconds between test frames.
WLE5_LINK_DBM   ?=
WLE5_LINK_SEC   ?=
# wle5node only: transmit power in dBm, default 0 in the program (the board cap of 10 dBm still applies;
# more only with the hand-written tag wle5_allow_high_power, which this Makefile never sets).
WLE5_TX_DBM     ?=
# wle5cw and wle5link transmit: 1 = an antenna or a 50 ohm load is connected (the only value
# accepted). Without it the programs print how to confirm and do not transmit.
WLE5_ANT_OK     ?=
WLE5_LDX        := $(if $(WLE5_TCXO_MV),-X cairnmesh/firmware/board/wle5.tcxoMV=$(WLE5_TCXO_MV)) $(if $(and $(WLE5_CW_DBM),$(filter wle5cw,$(CMD))),-X main.cwDBM=$(WLE5_CW_DBM)) $(if $(and $(WLE5_LINK_DBM),$(filter wle5link,$(CMD))),-X main.txDBM=$(WLE5_LINK_DBM)) $(if $(and $(WLE5_TX_DBM),$(filter wle5node,$(CMD))),-X main.txDBM=$(WLE5_TX_DBM)) $(if $(and $(WLE5_LINK_SEC),$(filter wle5link,$(CMD))),-X main.periodSec=$(WLE5_LINK_SEC)) $(if $(and $(WLE5_ANT_OK),$(filter wle5cw wle5link wle5node,$(CMD))),-X main.antOK=$(WLE5_ANT_OK)) $(if $(and $(filter 1,$(BENCH)),$(filter wle5node,$(CMD))),-X main.benchBuild=1)
# wle5node only: WLE5_TESTFILTER=1 builds the TEST receive filter in (go build tag cairn_testfilter:
# package rxfilter, the hook in node.Stack, the console command rxf; spec 5 TX is its key). Put on the
# command line, never taken from the environment. The image is named wle5-wle5node-testfilter.*; no
# other build has the tag, so no other build has a byte of it (the tag, not a link-time variable, because
# a variable cannot take code out of an image). wle5-build asserts it of every wle5node image
# (tools/wle5-assert-prod.sh); make wle5-assert-prod builds and checks both.
ifeq ($(origin WLE5_TESTFILTER),environment)
$(error WLE5_TESTFILTER comes from the environment: give it on the command line, make wle5-build CMD=wle5node WLE5_TESTFILTER=1)
endif
WLE5_TESTFILTER ?=
# wle5node only: WLE5_BENCH_ACTIONS=1 adds actions 0x02-0x05 from package
# testbed/benchactions (go build tag cairn_testbed). It is separate from
# WLE5_TESTFILTER, whose semantics remain the receive filter only. Command line
# only, like WLE5_TESTFILTER; production and dist never set it.
ifeq ($(origin WLE5_BENCH_ACTIONS),environment)
$(error WLE5_BENCH_ACTIONS comes from the environment: give it on the command line, make wle5-build CMD=wle5node WLE5_BENCH_ACTIONS=1)
endif
WLE5_BENCH_ACTIONS ?=
WLE5_NAME       := wle5-$(CMD)$(if $(WLE5_TESTFILTER),-testfilter)$(if $(WLE5_BENCH_ACTIONS),-bench)
WLE5_TAGS       := $(strip $(if $(filter pcb4,$(WLE5_BOARD)),wle5_pcb4) $(if $(WLE5_TESTFILTER),cairn_testfilter) $(if $(WLE5_BENCH_ACTIONS),cairn_testbed))
WLE5_ASSERT_MODE := $(if $(and $(WLE5_TESTFILTER),$(WLE5_BENCH_ACTIONS)),testfilter-bench,$(if $(WLE5_TESTFILTER),testfilter,$(if $(WLE5_BENCH_ACTIONS),bench,prod)))
WLE5_FLAGS      := -target=cairn-wle5 $(if $(WLE5_TAGS),-tags="$(WLE5_TAGS)") -stack-size=$(STACK_SIZE) $(if $(WLE5_OPT),-opt=$(WLE5_OPT)) $(if $(strip $(WLE5_LDX)),-ldflags="$(strip $(WLE5_LDX))")

wle5-check-env:
	@command -v $(TINYGO) >/dev/null 2>&1 || { echo "error: $(TINYGO) is not in PATH"; exit 1; }
	@test -e "$(TINYGOROOT_WLE5)/lib/clang" || { echo "error: $(TINYGOROOT_WLE5)/lib/clang missing"; exit 1; }
	@TINYGO_LOCK=tinygo-wle5.lock sh tools/tinygo.sh check "$(TINYGOROOT_WLE5)" "$(TINYGO)"

tinygo-wle5-apply:
	TINYGO_LOCK=tinygo-wle5.lock sh tools/tinygo.sh apply "$(TINYGOROOT_WLE5)"

wle5-build: wle5-check-env
	@if [ "$(CMD)" = wle5cw ] && [ -n "$(WLE5_CW_DBM)" ]; then \
		case "$(WLE5_CW_DBM)" in ''|*[!0-9+-]*) echo "error: WLE5_CW_DBM=$(WLE5_CW_DBM) is not a whole number"; exit 1;; esac; \
		[ "$(WLE5_CW_DBM)" -le 10 ] && [ "$(WLE5_CW_DBM)" -ge -9 ] || { echo "error: WLE5_CW_DBM=$(WLE5_CW_DBM) is outside -9..10 (10 dBm ceiling)"; exit 1; }; \
	fi
	@if [ "$(CMD)" = wle5node ] && [ "$(BENCH)" = 1 ]; then echo "note: BENCH=1 (from the command line or the environment): this is a BENCH build, it joins the network without read protection; never for deployment" >&2; fi
	@case "$(CMD)" in wle5cw|wle5link|wle5node) \
		if [ -z "$(WLE5_ANT_OK)" ]; then echo "note: WLE5_ANT_OK is not set: $(CMD) will not transmit (connect an antenna or 50 ohm load, then make WLE5_ANT_OK=1)" >&2; \
		elif [ "$(WLE5_ANT_OK)" != 1 ]; then echo "error: WLE5_ANT_OK=$(WLE5_ANT_OK): the only accepted value is 1"; exit 1; fi;; esac
	@if [ "$(CMD)" = wle5link ] && [ -n "$(WLE5_LINK_DBM)" ]; then \
		case "$(WLE5_LINK_DBM)" in ''|*[!0-9+-]*) echo "error: WLE5_LINK_DBM=$(WLE5_LINK_DBM) is not a whole number"; exit 1;; esac; \
		[ "$(WLE5_LINK_DBM)" -le 10 ] && [ "$(WLE5_LINK_DBM)" -ge -9 ] || { echo "error: WLE5_LINK_DBM=$(WLE5_LINK_DBM) is outside -9..10 (10 dBm ceiling)"; exit 1; }; \
	fi
	@if [ "$(CMD)" = wle5node ] && [ -n "$(WLE5_TX_DBM)" ]; then \
		printf '%s' "$(WLE5_TX_DBM)" | grep -Eq '^[+-]?[0-9]+$$' || { echo "error: WLE5_TX_DBM=$(WLE5_TX_DBM) is not a whole number (an optional sign, then digits)"; exit 1; }; \
		[ "$(WLE5_TX_DBM)" -le 10 ] && [ "$(WLE5_TX_DBM)" -ge -9 ] || { echo "error: WLE5_TX_DBM=$(WLE5_TX_DBM) is outside -9..10 (10 dBm ceiling)"; exit 1; }; \
	fi
	@if [ "$(CMD)" = wle5node ]; then echo "note: wle5node transmit power $(if $(WLE5_TX_DBM),$(WLE5_TX_DBM),0 (default)) dBm" >&2; fi
	@if [ "$(CMD)" = wle5link ] && [ -n "$(WLE5_LINK_SEC)" ]; then \
		case "$(WLE5_LINK_SEC)" in ''|*[!0-9]*) echo "error: WLE5_LINK_SEC=$(WLE5_LINK_SEC) is not a whole number of seconds"; exit 1;; esac; \
		[ "$(WLE5_LINK_SEC)" -ge 1 ] && [ "$(WLE5_LINK_SEC)" -le 3600 ] || { echo "error: WLE5_LINK_SEC=$(WLE5_LINK_SEC) is outside 1..3600"; exit 1; }; \
	fi
	@if [ -n "$(WLE5_TESTFILTER)" ]; then \
		[ "$(WLE5_TESTFILTER)" = 1 ] && [ "$(CMD)" = wle5node ] || { echo "error: WLE5_TESTFILTER=1 is the only accepted value, and only with CMD=wle5node"; exit 1; }; \
		echo "note: WLE5_TESTFILTER=1: a TEST build with the receive filter (console: rxf); image $(BUILD_DIR)/$(WLE5_NAME).elf; never for a full-network test or a deployment" >&2; \
	fi
	@if [ -n "$(WLE5_BENCH_ACTIONS)" ]; then \
		[ "$(WLE5_BENCH_ACTIONS)" = 1 ] && [ "$(CMD)" = wle5node ] || { echo "error: WLE5_BENCH_ACTIONS=1 is the only accepted value, and only with CMD=wle5node"; exit 1; }; \
		echo "note: WLE5_BENCH_ACTIONS=1: a TESTBED build with actions 0x02-0x05; image $(BUILD_DIR)/$(WLE5_NAME).elf; never for deployment" >&2; \
	fi
	@mkdir -p "$(BUILD_DIR)"
	TINYGOROOT=$(TINYGOROOT_WLE5) $(TINYGO) build $(WLE5_FLAGS) -o "$(BUILD_DIR)/$(WLE5_NAME).elf" ./cmd/$(CMD)
	@if [ "$(CMD)" = wle5node ]; then sh tools/wle5-assert-prod.sh $(WLE5_ASSERT_MODE) "$(BUILD_DIR)/$(WLE5_NAME).elf" || { rm -f "$(BUILD_DIR)/$(WLE5_NAME).elf"; echo "removed $(BUILD_DIR)/$(WLE5_NAME).elf: it failed the build assertions" >&2; exit 1; }; fi

# For wle5node the image must also end below the journals, key and counter pages (FLASH_LIMIT).
wle5-size: wle5-check-env
	TINYGOROOT=$(TINYGOROOT_WLE5) $(TINYGO) build -size short $(WLE5_FLAGS) -o /dev/null ./cmd/$(CMD)
	@if [ "$(CMD)" = wle5node ]; then \
		mkdir -p "$(BUILD_DIR)"; \
		TINYGOROOT=$(TINYGOROOT_WLE5) $(TINYGO) build $(WLE5_FLAGS) -o "$(BUILD_DIR)/$(WLE5_NAME).bin" ./cmd/$(CMD) || exit 1; \
		sh tools/wle5-assert-prod.sh $(WLE5_ASSERT_MODE) "$(BUILD_DIR)/$(WLE5_NAME).bin" || { rm -f "$(BUILD_DIR)/$(WLE5_NAME).bin"; echo "removed $(BUILD_DIR)/$(WLE5_NAME).bin: it failed the build assertions" >&2; exit 1; }; \
		size=$$(stat -c %s "$(BUILD_DIR)/$(WLE5_NAME).bin"); \
		if [ $$size -ge $$(($(FLASH_LIMIT))) ]; then echo "error: image $$size B reaches the journal, key and counter pages ($(FLASH_LIMIT))"; exit 1; fi; \
		echo "image $$size B, below the journal, key and counter pages ($(FLASH_LIMIT))"; \
	fi

wle5-allocs: wle5-check-env
	TINYGOROOT=$(TINYGOROOT_WLE5) $(TINYGO) build $(WLE5_FLAGS) -print-allocs='cairnmesh/firmware' -o /dev/null ./cmd/$(CMD)

# Builds the production, receive-filter, bench-action and combined wle5node
# images and asserts their markers (each wle5-build runs the assert script).
wle5-assert-prod:
	$(MAKE) --no-print-directory wle5-build CMD=wle5node
	$(MAKE) --no-print-directory wle5-build CMD=wle5node WLE5_TESTFILTER=1
	$(MAKE) --no-print-directory wle5-build CMD=wle5node WLE5_BENCH_ACTIONS=1
	$(MAKE) --no-print-directory wle5-build CMD=wle5node WLE5_TESTFILTER=1 WLE5_BENCH_ACTIONS=1

# Build and assert the production gateway and its bench-fault counterpart.
gateway-assert-prod:
	$(MAKE) --no-print-directory build CMD=gateway
	sh tools/gateway-assert-prod.sh prod "$(BUILD_DIR)/gateway.elf"
	$(MAKE) --no-print-directory build CMD=gateway GATEWAY_BENCH_FAULT=radio1
	sh tools/gateway-assert-prod.sh testbed "$(BUILD_DIR)/gateway-testbed.elf"

# All image assertions. This needs the U083 and WLE5 TinyGo trees; host CI
# covers the build-tag mapping through tools/buildbounds instead.
assert-prod: wle5-assert-prod gateway-assert-prod

# SWD with OpenOCD and a J-Link (tools/wle5-openocd.sh: connect under reset, the E77's
# factory firmware turns SWD off). The user approves every flash; wle5-flash refuses
# until a factory backup of this module exists (make wle5-backup WLE5_BACKUP=backups/<name>.bin).
WLE5_BACKUP ?= backups/e77-factory-$(shell date +%Y%m%d-%H%M%S).bin
BENCH_LOCK  ?= $(HOME)/.cache/cairnmesh/bench.lock

wle5-backup:
	@mkdir -p "$(dir $(WLE5_BACKUP))"
	flock -n $(BENCH_LOCK) sh tools/wle5-openocd.sh dump "$(WLE5_BACKUP)"

wle5-flash:
	@ls backups/e77-factory-*.bin >/dev/null 2>&1 || { echo "error: no backups/e77-factory-*.bin: make wle5-backup first"; exit 1; }
	@test -f "$(BUILD_DIR)/$(WLE5_NAME).elf" || { echo "error: build it first: make wle5-build CMD=$(CMD) ..."; exit 1; }
	@if [ "$(CMD)" = wle5node ]; then sh tools/wle5-assert-prod.sh $(WLE5_ASSERT_MODE) "$(BUILD_DIR)/$(WLE5_NAME).elf" || { echo "error: not flashing $(BUILD_DIR)/$(WLE5_NAME).elf: its build marks do not match its name"; exit 1; }; fi
	flock -n $(BENCH_LOCK) sh tools/wle5-openocd.sh flash "$(BUILD_DIR)/$(WLE5_NAME).elf"

# On the bench: tools/wle5-flash-check.sh and wle5-gdb.sh take the bench lock themselves.
wle5-flash-check:
	sh tools/wle5-flash-check.sh $(WLE5_CHECK_ARGS)

wle5-gdb:
	sh tools/wle5-gdb.sh $(WLE5_GDB_ARGS)

test:
	go test ./proto/ ./journal/ ./mesh/ ./node/... ./ext/ ./radio/... ./gateway/ ./usb/ ./console/ ./keystore/ ./link/ ./tools/... ./testbed/... ./hw/subghz/ ./cmd/uarthello/hellotext/ ./cmd/wle5check/checktext/ ./cmd/wle5cw/cwpower/ ./cmd/wle5node/wiring/ ./cmd/wle5link/linktext/ ./board/wle5/safety/ ./board/wle5/adapt/ ./rxfilter/ ./stackwater/
	go test -tags cairn_testfilter ./node/ ./rxfilter/ ./cmd/wle5node/wiring/

vectors:
	cd cmd/selftest && go generate .

ifeq ($(filter $(CMD),uarthello wle5check wle5cw wle5link wle5node wle5probe),)
build: check-env
	@if [ -n "$(GATEWAY_BENCH_FAULT)" ]; then \
		[ "$(CMD)" = gateway ] || { echo "error: GATEWAY_BENCH_FAULT is only for CMD=gateway"; exit 1; }; \
		case "$(GATEWAY_BENCH_FAULT)" in radio1|hang) ;; *) echo "error: GATEWAY_BENCH_FAULT must be radio1 or hang"; exit 1;; esac; \
	fi
	@mkdir -p "$(BUILD_DIR)"
	$(TINYGO) build $(TGFLAGS) -o "$(BUILD_DIR)/$(CMD_OUT).elf" ./cmd/$(CMD)
	$(TINYGO) build $(TGFLAGS) -o "$(BUILD_DIR)/$(CMD_OUT).bin" ./cmd/$(CMD)

size: build
	$(TINYGO) build -size short $(TGFLAGS) -o /dev/null ./cmd/$(CMD)
	@size=$$(stat -c %s "$(BUILD_DIR)/$(CMD_OUT).bin"); \
	if [ $$size -ge $$(($(FLASH_LIMIT))) ]; then echo "error: image $$size B reaches the journal, key and counter pages ($(FLASH_LIMIT))"; exit 1; fi; \
	echo "image $$size B, below the journal, key and counter pages ($(FLASH_LIMIT))"

allocs: check-env
	$(TINYGO) build $(TGFLAGS) -print-allocs='cairnmesh/firmware' -o /dev/null ./cmd/$(CMD)

flash: size
	PATH="$(CUBEPROG_BIN):$$PATH" $(TINYGO) flash $(TGFLAGS) ./cmd/$(CMD)
else
build:
	$(MAKE) --no-print-directory wle5-build CMD=$(CMD)

size:
	$(MAKE) --no-print-directory wle5-size CMD=$(CMD)

allocs:
	$(MAKE) --no-print-directory wle5-allocs CMD=$(CMD)

flash:
	$(MAKE) --no-print-directory wle5-build CMD=$(CMD)
	$(MAKE) --no-print-directory wle5-flash CMD=$(CMD)
endif

# A deployment image (no keys, BENCH=0) with its checksums and the toolchain it was built with.
dist:
	@test "$(BENCH)" = 0 || { echo "error: dist builds the deployment image: BENCH=0"; exit 1; }
	@test -z "$(WLE5_TESTFILTER)" || { echo "error: dist builds the production image: WLE5_TESTFILTER must be unset"; exit 1; }
	@test -z "$(WLE5_BENCH_ACTIONS)" || { echo "error: dist builds the production image: WLE5_BENCH_ACTIONS must be unset"; exit 1; }
	@rm -rf -- "$(BUILD_DIR)/dist"
	$(MAKE) --no-print-directory wle5-build CMD=wle5node
	$(MAKE) --no-print-directory wle5-size CMD=wle5node
	@mkdir -p "$(BUILD_DIR)/dist"
	cp "$(BUILD_DIR)/wle5-wle5node.elf" "$(BUILD_DIR)/wle5-wle5node.bin" "$(BUILD_DIR)/dist/"
	@{ $(TINYGO) version; grep -E '^(version|commit) ' patches/tinygo-wle5.lock; git rev-parse HEAD; } > "$(BUILD_DIR)/dist/BUILDINFO"
	cd "$(BUILD_DIR)/dist" && sha256sum wle5-wle5node.elf wle5-wle5node.bin BUILDINFO > SHA256SUMS && cat SHA256SUMS

selftest: check-env
	@mkdir -p "$(RESULTS)"
	$(MAKE) --no-print-directory size CMD=selftest
	python3 tools/loadreport.py capture $(SERIAL_PORT) $(RESULTS)/selftest.log 60 & pid=$$!; \
	sleep 1; PATH="$(CUBEPROG_BIN):$$PATH" $(TINYGO) flash $(TGFLAGS) ./cmd/selftest; wait $$pid
	@grep -E '^(R|V .*FAIL|A |aes)' $(RESULTS)/selftest.log

monitor:
	python3 tools/monitor.py $(SERIAL_PORT) $(BAUD) $(MONITOR_SECS)

info: check-env
	"$(STM32_PROG)" -c port=SWD

# Never overwrites a backup; factory-backup.bin is read-only.
backup: check-env
	@test ! -e "$(BACKUP)" || { echo "error: $(BACKUP) exists; pick another BACKUP="; exit 1; }
	@mkdir -p "$(dir $(BACKUP))"
	"$(STM32_PROG)" -c port=SWD -u $(FLASH_ADDR) $(FLASH_SIZE) "$(BACKUP)"
	chmod a-w "$(BACKUP)"

restore: check-env
	@test -f "$(BACKUP)" || { echo "error: name an existing backup: make restore BACKUP=backups/<file>.bin"; exit 1; }
	"$(STM32_PROG)" -c port=SWD -w "$(BACKUP)" $(FLASH_ADDR) -v -rst

restore-factory: check-env
	@test -f "$(FACTORY)" || { echo "error: factory image not found: $(FACTORY)"; exit 1; }
	"$(STM32_PROG)" -c port=SWD -w "$(FACTORY)" $(FLASH_ADDR) -v -rst

erase:
	@test "$(CONFIRM)" = yes || { echo "This erases the whole chip, the FCNT counter pages too."; echo "Run: make erase CONFIRM=yes"; exit 1; }
	@$(MAKE) --no-print-directory check-env
	"$(STM32_PROG)" -c port=SWD -e all -rst

clean:
	rm -rf "$(BUILD_DIR)"
