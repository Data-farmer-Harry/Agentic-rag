# Computer Workspace

Files placed here are mounted read-only into the standard Docker app as the `workspace` root.
HermesGraph can list, read, and search supported text/code files plus PDF, DOCX, and XLSX
documents. Hidden paths, credential-like filenames, private-key formats, symbolic links, and
unsupported binary files are blocked.

To expose another directory, add an explicit read-only bind mount and update
`COMPUTER_WORKSPACE_ROOTS`. Do not mount a home directory, password store, SSH directory, or
repository containing live secrets.
