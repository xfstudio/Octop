//go:build !windows

package main

import (
	"errors"
	"os/exec"
	"syscall"
)

func isAddressInUse(err error) bool {
	return errors.Is(err, syscall.EADDRINUSE)
}

func configureProcGroup(cmd *exec.Cmd) {
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
}

func killProcessTree(cmd *exec.Cmd) {
	_ = syscall.Kill(-cmd.Process.Pid, syscall.SIGTERM)
	_ = cmd.Wait()
}
