# OmaGTD - A description for humans

### Rationale and background

The program comes from the interest to leave macOS (if you are interested, you can see my [auinobackonlinux@mastodon.social](https://mastodon.social/@auinobackonlinux) account on Mastodon, documenting the process).
In my attempts to move back (from 2011) to Linux, I discovered [Omarchy Linux](https://omarchy.org).
Due to its AI and agents oriented approach, I found it an interesting approach.

### The need

What is currently sticking me with the Apple ecosystem are (at least, to date) essentially a few apps: one of them is [OmniFocus](https://www.omnigroup.com/omnifocus/), an app for [GTD](https://en.wikipedia.org/wiki/Getting_Things_Done) and tasks management.
Hence, to overcome such limitation, I started implementing a custom GTD system which takes the best from my experience from OmniFocus, but also exploiting the Omarchy and open-source phylosophy.

### The vibe coding approach

The entire implementation has been done through vibe coding, by using [OpenCode](https://opencode.ai): my role was to define the requisites and validate the results, in a cyclic and (longer than expected) process.
The following models have been mainly used:
* [Big Pickle](https://opencode.ai/v2/docs/console/models/) by OpenCode Zen
+ `qwen3.8:27b-mlx` running in an [Ollama](https://ollama.com) instance local in the LAN

Being the project expected to be released publicly, as open-source software, the use of a cloud-based system (i.e., Big Pickle) does not have any impact on potential data sharing issues.

### The app features

The app is actually an [Omarchy plugin](https://omarchy.org/manual/shell-plugins/) with the following features:
* import from OmniFocus
* data export and database structure
* cli based interactions (e.g., may be useful for integrations with `cron`)
* keyboard-first approach

### Import from OmniFocus

First, run the following activties to move from OmniFocus to OmaGTD:
1. Open OmniFocus and click on `Files`, `Export` to save your data to a `.ofocus` file (which will be seen by Linux as a folder)
2. Move the `.ofocus` file to the Linux host
3. Import all the tasks in OmaGTD:
```
~/.config/omarchy/plugins/com.auino.omagtd/bin/gtd import --format ofocus --tag ofimport <yourexportfile.ofocus>
```
4. Review your imported files by opening the `ofimport` context in OmaGTD

### Layout and structure

Three components are implemented:
* the top bar indicators, showing a `GTD` text along with a `n/m` indicator:
while `n` identifies the tasks expired today, `m` identifies the total number of tasks expiring today. The entire indicator is red coloured if at least one task expired, orange coloured if at least one task is flagged as urgent (red colouring takes the priority) 
* the panel view, which can be opened by clicking on the top bar indicator:
from such view, all today's tasks are visible; also, it's possible to insert a new task
* the full screen view, which can be opened through a dedicated `m` shortcut in the panel, or through command line/system shortcut:
the screen is divided in multiple sections:
  * top part, with a big capture input field, plus a search and settings link
  * left part, with a navigation section and a general view for either projects or contexts
  * central part, with details on the currently selected view
  * right part, with details on specific tasks
  * bottom part, with details on app shortcut or ongoing actions

In full screen mode, left-to-right parts navigation uses left/right arrows, while navigation across elements in the same part uses tab or up/down arrows.

### New task creation syntax

Tasks can be created by using a single text syntax populating specific fields: the basic syntax is the following one:

```
task_title #context /project/ <defer_date >deadline_date
```

While project names can have space characters (e.g., `Single actions`), this is not true for contexts.
Also, contexts names are always converted to lower case.
Both project and context names can be nested with a `>` symbol: e.g., `Personal>Errands`.

### Relevant contents

* `~/.config/omarchy/plugins/com.auino.omagtd/` the folder including the plugin
* `~/.config/omarchy/plugins/com.auino.omagtd/bin/gtd` the command line version of the app (use `-h` for help)
* `~/.local/share/omagtd/` the folder with your local data, including inner database and attachments

### Interaction with other systems

Currently, interaction is quite limited, at least in the current version of the app.
While apps like OmniFocus use a cloud-based service to manage sync across devices, OmaGTD makes use of a local SQLite3 database.
Therefore, interaction between different devices can still be done, but it requires CLI-based commands (e.g., integrated with `cron`), to either create tasks or export them, e.g., by using a shared folder.

### The limitations

While the app represents a usable solution, it lacks of relevant features.
At first, the integration with mobile and other systems (e.g., through a web interface) is missing.
Also, no sync between devices is implemented.
The GUI follows the Omarchy ecosystem, but it is not appealing as the OmniFocus GUI.
Review is not properly implemented (yet).
For other bugs, just report them as issues in this repository.
Please also report any suggestion or improvement.

Given the current status of the app, please use it at your own risk.
