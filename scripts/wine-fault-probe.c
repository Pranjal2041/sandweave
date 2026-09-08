#include <stdio.h>
#include <windows.h>

static volatile LONG *page;
static LONG handled;

static LONG CALLBACK recover(EXCEPTION_POINTERS *exception) {
    EXCEPTION_RECORD *record = exception->ExceptionRecord;
    printf("exception=%08lx parameters=%lu operation=%llu address=%p\n",
           record->ExceptionCode, record->NumberParameters,
           (unsigned long long)record->ExceptionInformation[0],
           (void *)record->ExceptionInformation[1]);
    fflush(stdout);
    if (record->ExceptionCode != EXCEPTION_ACCESS_VIOLATION ||
        record->NumberParameters != 2 || record->ExceptionInformation[0] != 1 ||
        record->ExceptionInformation[1] != (ULONG_PTR)page) ExitProcess(10);
    DWORD old_protection;
    if (!VirtualProtect((void *)page, 4096, PAGE_READWRITE, &old_protection)) ExitProcess(11);
    ++handled;
    return EXCEPTION_CONTINUE_EXECUTION;
}

int main(void) {
    page = VirtualAlloc(NULL, 4096, MEM_COMMIT | MEM_RESERVE, PAGE_NOACCESS);
    if (!page || !AddVectoredExceptionHandler(1, recover)) return 2;
    *page = 41;
    InterlockedIncrement(page);
    printf("resumed=%ld value=%ld\n", handled, *page);
    return handled == 1 && *page == 42 ? 0 : 3;
}
