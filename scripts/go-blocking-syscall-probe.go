// Compare Go scheduling during a bounded host wait using the two syscall APIs.
package main

import (
	"encoding/json"
	"os"
	"os/exec"
	"runtime"
	"syscall"
	"time"
	"unsafe"
)

func main() {
	if len(os.Args) != 2 || (os.Args[1] != "raw" && os.Args[1] != "scheduled") {
		os.Stderr.WriteString("usage: go-blocking-syscall-probe raw|scheduled\n")
		os.Exit(2)
	}
	runtime.GOMAXPROCS(2)
	read, write, err := os.Pipe()
	if err != nil {
		panic(err)
	}
	defer read.Close()
	writer := exec.Command("sh", "-c", "sleep 2; printf x >&3")
	writer.ExtraFiles = []*os.File{write}
	if err := writer.Start(); err != nil {
		panic(err)
	}
	write.Close()
	entered := make(chan struct{})
	finished := make(chan syscall.Errno, 1)
	go func() {
		var value byte
		close(entered)
		var errno syscall.Errno
		if os.Args[1] == "raw" {
			_, _, errno = syscall.RawSyscall(syscall.SYS_READ, read.Fd(), uintptr(unsafe.Pointer(&value)), 1)
		} else {
			_, _, errno = syscall.Syscall(syscall.SYS_READ, read.Fd(), uintptr(unsafe.Pointer(&value)), 1)
		}
		finished <- errno
	}()
	<-entered
	time.Sleep(100 * time.Millisecond)
	start := time.Now()
	runtime.GC()
	duration := time.Since(start)
	if errno := <-finished; errno != 0 {
		panic(errno)
	}
	if err := writer.Wait(); err != nil {
		panic(err)
	}
	json.NewEncoder(os.Stdout).Encode(map[string]any{"mode": os.Args[1], "gc_seconds": duration.Seconds()})
}
