
/* STM32WLE5CCU6: 256 KB flash (128 pages of 2 KB), 64 KB SRAM (SRAM1 32 KB + SRAM2 32 KB,
   contiguous from 0x20000000; RM0461 2.3, DS13105 table 1). Keep the end of the image below
   the journals, the key record and the FCNT pages (board.RecordsPage, board.KeyPage ...,
   FLASH_LIMIT in the Makefile). */
MEMORY
{
    FLASH_TEXT (rw) : ORIGIN = 0x08000000, LENGTH = 256K
    RAM (xrw)       : ORIGIN = 0x20000000, LENGTH = 64K
}

_stack_size = 4K;
_stack_start = ORIGIN(RAM);

INCLUDE "targets/arm.ld"
