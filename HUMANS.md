# OmaGTD - A description for humans

### Rationale and background

The program comes from the interest to leave macOS (if you are interested, you can see my [auinobackonlinux@mastodon.social](https://mastodon.social/@auinobackonlinux) account on Mastodon, documenting the process).
In my attempts to move back (from 2011) to Linux, I discovered [Omarchy Linux](https://omarchy.org).
Due to its AI and agents oriented approach, I found it an interesting approach.

### The need

What is currently sticking me with the Apple ecosystem are (at least, to date) essentially two apps:
* [OmniFocus](https://www.omnigroup.com/omnifocus/), for [GTD](https://en.wikipedia.org/wiki/Getting_Things_Done) and tasks management
* [MoneyWiz](https://www.wiz.money), as a personal finance tool

While I will not go deeper on explaining how such solutions are great (at least, imho), referring to this repository, I decided to focus on the first one.
Hence, I started implementing a custom GTD system which takes the best from my experience from OmniFocus, but also exploiting the Omarchy and open-source phylosophy.

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

### The limitations

While the app represents a usable solution, it lacks of relevant features.
At first, the integration with mobile and other systems (e.g., through a web interface) is missing.
Also, no sync between devices is implemented.
The GUI follows the Omarchy ecosystem, but it is not appealing as the OmniFocus GUI.
Review is not properly implemented (yet).
For other bugs, just report them as issues in this repository.
Please also report any suggestion or improvement.

Given the current status of the app, please use it at your own risk.
