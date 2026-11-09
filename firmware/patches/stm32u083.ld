MEMORY
{
    FLASH_TEXT (rw) : ORIGIN = 0x08000000, LENGTH = 256K
    RAM (xrw)       : ORIGIN = 0x20000000, LENGTH = 32K
}

_stack_size = 2K;

INCLUDE "targets/arm.ld"
