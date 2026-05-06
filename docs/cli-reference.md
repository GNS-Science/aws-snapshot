# CLI Reference

## Command tree

Use this as a quick map of the CLI surface. Detailed options/arguments for each
command are listed in the generated reference below.

```text
backup
├── check
├── schedule
│   ├── show
│   ├── health
│   ├── add
│   ├── remove
│   ├── enable
│   └── disable
├── setup
│   ├── inventory
│   └── iam
│       ├── source-roles
│       └── backup-batch-role
├── run
├── restore
├── test
├── status
├── events
├── report
├── costs
└── config
    ├── show
    ├── validate
    ├── push
    └── pull
```

## Full command reference

::: mkdocs-click
    :module: nzshm_backup.cli
    :command: click_app
    :depth: 1
    :style: table
    :list_subcommands: true
