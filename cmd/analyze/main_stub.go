//go:build !darwin

// Linux keeps the useful, safe part of Mole's analyzer: a bounded, read-only
// disk report suitable for development machines.  macOS-specific TUI and
// deletion code intentionally stays behind the darwin build tag.
package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
	"sort"
)

type linuxEntry struct {
	Name  string `json:"name"`
	Path  string `json:"path"`
	Size  int64  `json:"size"`
	IsDir bool   `json:"is_dir"`
}

type linuxReport struct {
	Path       string       `json:"path"`
	Overview   bool         `json:"overview"`
	Entries    []linuxEntry `json:"entries"`
	LargeFiles []linuxEntry `json:"large_files"`
	TotalSize  int64        `json:"total_size"`
	TotalFiles int64        `json:"total_files"`
}

func main() {
	jsonMode := flag.Bool("json", false, "output analysis as JSON")
	flag.Parse()
	target := "."
	if flag.NArg() > 0 {
		target = flag.Arg(0)
	}
	abs, err := filepath.Abs(target)
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	report, err := scanLinux(abs)
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	if *jsonMode {
		_ = json.NewEncoder(os.Stdout).Encode(report)
		return
	}
	fmt.Printf("Linux disk analysis: %s\nTotal: %.1f MB in %d files\n", report.Path, float64(report.TotalSize)/1e6, report.TotalFiles)
	for _, entry := range report.LargeFiles {
		fmt.Printf("%.1f MB\t%s\n", float64(entry.Size)/1e6, entry.Path)
	}
}

func scanLinux(root string) (linuxReport, error) {
	info, err := os.Stat(root)
	if err != nil {
		return linuxReport{}, err
	}
	report := linuxReport{Path: root, Overview: false}
	if info.IsDir() {
		entries, err := os.ReadDir(root)
		if err != nil {
			return report, err
		}
		for _, entry := range entries {
			path := filepath.Join(root, entry.Name())
			size := treeSize(path, &report.TotalFiles, &report.LargeFiles)
			report.TotalSize += size
			report.Entries = append(report.Entries, linuxEntry{Name: entry.Name(), Path: path, Size: size, IsDir: entry.IsDir()})
		}
	} else {
		report.TotalSize = fileSize(info)
		report.TotalFiles = 1
		report.Entries = []linuxEntry{{Name: info.Name(), Path: root, Size: report.TotalSize, IsDir: false}}
	}
	sort.Slice(report.Entries, func(i, j int) bool { return report.Entries[i].Size > report.Entries[j].Size })
	sort.Slice(report.LargeFiles, func(i, j int) bool { return report.LargeFiles[i].Size > report.LargeFiles[j].Size })
	if len(report.LargeFiles) > 50 {
		report.LargeFiles = report.LargeFiles[:50]
	}
	return report, nil
}

func treeSize(path string, files *int64, largeFiles *[]linuxEntry) int64 {
	var total int64
	_ = filepath.WalkDir(path, func(filePath string, entry fs.DirEntry, err error) error {
		if err != nil || entry.Type()&os.ModeSymlink != 0 {
			if err != nil || entry.IsDir() {
				return fs.SkipDir
			}
			return nil
		}
		if entry.Type().IsRegular() {
			if info, statErr := entry.Info(); statErr == nil {
				size := fileSize(info)
				total += size
				*files = *files + 1
				*largeFiles = append(*largeFiles, linuxEntry{Name: entry.Name(), Path: filePath, Size: size, IsDir: false})
			}
		}
		return nil
	})
	return total
}

func fileSize(info fs.FileInfo) int64 {
	if info == nil || !info.Mode().IsRegular() {
		return 0
	}
	return info.Size()
}
