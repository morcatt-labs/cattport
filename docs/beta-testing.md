# Beta testing

Thanks for trying Cattport. A short report is useful even if everything works. Please include your NAS model, DSM version and whether it's an original Synology NAS or Arc Loader.

## Before you start

- Use an administrator account.
- Have a second account that isn't an administrator, if you can.
- Cattport doesn't touch your containers. Removing it takes its tiles out of DSM.

## Checklist

**Install**
- [ ] It installs from Package Center → Manual Install
- [ ] Cattport opens in a DSM window, in your language
- [ ] Diagnostics shows the right DSM version

**Adding apps**
- [ ] Add from Docker lists your containers
- [ ] Add a link works, with or without typing `https://`
- [ ] Icons are suggested, and you can choose one from your computer and from File Station
- [ ] The unsaved counter goes up, and nothing appears in the DSM menu yet

**Save to DSM**
- [ ] The window closes and comes back by itself
- [ ] Apps are in the main menu, including in another browser
- [ ] Each tile opens the right address locally
- [ ] If you use a domain, the domain address opens from outside your network

**Who sees it**
- [ ] A non-administrator sees Everyone tiles after reloading DSM
- [ ] A non-administrator doesn't see Only admins tiles
- [ ] Hide removes a tile for everyone
- [ ] Deleting a tile removes it for everyone after saving

**Uninstall**
- [ ] Keep data: your apps come back after reinstalling
- [ ] Delete data: Cattport starts empty after reinstalling

## Most useful to test

- DSM 7.0, 7.1 and 7.2
- ARM and other non-x86 models
- Many containers, or a NAS without Container Manager
- Browsers other than Chrome

## Reporting a problem

Use [Report a problem](https://github.com/morcatt-labs/cattport/issues/new/choose). Remove private addresses from screenshots before posting them.
