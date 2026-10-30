# Deploying the home node

> Hearth is a development project with an unaudited protocol. Run it for people who understand that, and if you find something wrong, [open a PR](../CONTRIBUTING.md).

The home node is the member that never sleeps. It holds every post and image, relays between friends who are never online at the same time, and is the only node that accepts invites. It runs on your on-prem Linux VM as a locked-down systemd service, listening on TCP port 7777.

You need:
- a Linux VM with systemd and Python 3.12 or newer. Ubuntu 24.04 ships 3.12, which is what this guide was tested on (in a container). Debian 12's 3.11 is too old; Debian 13 works.
- about 300 MiB of free RAM while the service starts (Argon2id; see step 3), and disk space for everyone's photos (the quota defaults to 20 GiB).
- a way for friends outside your network to reach the VM: see "Reachability" first.

Every command below runs on the VM unless it says otherwise. Replace `hearth.example.net` with your own hostname.

## Reachability: check this first

Friends' laptops must open a TCP connection to your VM's port 7777 from anywhere. Compare two addresses:
- the WAN or Internet IP your router's admin page shows;
- what the world sees: run `curl -4 https://ifconfig.me` on the VM.

If they're the same, you have a public IP: use **path A** (port forwarding). If the router's WAN IP is in 100.64.0.0/10 (100.64.x.x to 100.127.x.x), or another private range, or the two differ, your ISP uses carrier-grade NAT and nothing can reach you from outside. Use **path B** (a small VPS with a WireGuard tunnel).

If you can only reach the VM through Tailscale (its only usable address is a 100.x Tailscale IP) and the tailnet is yours to configure, use **path C** (Tailscale Funnel). It's free and needs no second machine. Friends still install nothing but Hearth. If the machine sits on someone else's tailnet, where you can't turn Funnel on, give Hearth a VM of its own on your tailnet: see "Path C in a VM of its own".

Pick before you send the first real invite. Each friend keeps the address from their link in their own `config.json`, so changing paths later means every friend edits `home_host` by hand.

Whichever path, friends use a **hostname**, never a raw IP: invite links carry it, and a hostname can follow a changing IP.
- Path A: most home IPs change now and then, so get a free dynamic-DNS name. Many routers have a DDNS setting that keeps it updated.
- Path B: point a DNS name at the VPS's fixed IP.
- Path C: Funnel gives you one, `<vm>.<tailnet>.ts.net`.

## Install

**1. A user for the service, with no login shell, and its data folder.**
```bash
sudo useradd --system --user-group --home-dir /var/lib/hearth --shell /usr/sbin/nologin hearth
sudo install -d -o hearth -g hearth -m 0700 /var/lib/hearth
```
systemd's `StateDirectory=hearth` would create `/var/lib/hearth` on the first start, but `init` needs it before that.

**2. Python and Hearth, in a virtualenv under /opt/hearth.**
```bash
sudo apt install -y python3 python3-venv git
sudo git clone https://github.com/iffayush/hearth /opt/hearth
sudo python3 -m venv /opt/hearth/.venv
sudo /opt/hearth/.venv/bin/pip install /opt/hearth
```
No GitHub copy yet? Copy the project folder to `/opt/hearth` instead (for example `scp -r`), without its `.venv`.

To keep the commands below short, add this alias to your own `~/.bashrc` on the VM. It runs Hearth as the `hearth` user:
```bash
alias hearth='sudo -u hearth /opt/hearth/.venv/bin/python -m hearth'
```
Always run Hearth commands as `hearth` (the alias does). Hearth refuses a data folder that belongs to another user. Without that check, a plain `sudo` could leave files owned by root that the service can't open.

**3. Measure Argon2id on this VM.**
```bash
hearth debug bench-kdf
```
Every start unlocks two key files. The default, `moderate`, uses 256 MiB of RAM and takes about 1 s per start on a laptop. Keep it unless a start here takes more than about 10 s, or the VM can't spare 256 MiB while it starts. In that case, add `--kdf interactive` (64 MiB) to the `init` in step 5, and note the numbers in `docs/phase-8.md`.

**4. The passphrase file.** The service can't type a passphrase at boot, so it reads it from a file only `hearth` can read. Make a strong random one:
```bash
sudo install -d -o root -g hearth -m 0750 /etc/hearth
sudo sh -c 'umask 077; /opt/hearth/.venv/bin/python -c "import secrets; print(secrets.token_urlsafe(32))" > /etc/hearth/passphrase'
sudo chown hearth:hearth /etc/hearth/passphrase
sudo cat /etc/hearth/passphrase
```
Copy the passphrase into your password manager **now**. Backups never include it, and without it a backup is useless.

**5. Create the identity and the group.**
```bash
hearth init --profile home --data-dir /var/lib/hearth --passphrase-file /etc/hearth/passphrase
hearth create-group --data-dir /var/lib/hearth --passphrase-file /etc/hearth/passphrase
```

**6. Install and start the service.**
```bash
sudo install -m 0644 /opt/hearth/deploy/hearth-home.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now hearth-home
sudo systemctl status hearth-home
```
The log should end with `Listening on 0.0.0.0:7777`. Reading the service's log takes `sudo` unless your account is in the `adm` or `systemd-journal` group. To follow it: `sudo journalctl -u hearth-home -f`. `enable` makes it start at every boot.

The unit file explains each sandbox line. `systemd-analyze security hearth-home` rates it "1.5 OK", where the spec's minimum list alone scores 5.0. Python's log levels don't reach the journal's own priorities, so find problems with `sudo journalctl -u hearth-home | grep -E "WARNING|ERROR"`.

**7. Firewall: port 7777 for everyone, SSH only from your own network.** Add the SSH rule first, or `enable` will cut off your session:
```bash
sudo ufw allow from 192.168.1.0/24 to any port 22 proto tcp
sudo ufw allow 7777/tcp
sudo ufw enable
```
(Use your own LAN range instead of `192.168.1.0/24`.)

Path C differs: skip `ufw allow 7777/tcp`, because only tailscaled should reach 7777. If you SSH in over Tailscale, also run `sudo ufw allow in on tailscale0 to any port 22 proto tcp` before `enable`, or you may lock yourself out.

**8. Invite friends.**
```bash
hearth invite --data-dir /var/lib/hearth --host hearth.example.net:7777
```
Path C uses its Funnel name instead: `--host tls://hearth-vm.tail1234.ts.net:443`. To skip `--host` next time, set `"public_host": "hearth.example.net:7777"` (or the `tls://` address) in `/var/lib/hearth/config.json`. Send each link to one friend, privately. The friend then runs `join` and `run` on their laptop (see the README).

## Path A: port forwarding

1. In the router, give the VM a fixed LAN address: a DHCP reservation.
2. Forward TCP port 7777 from the WAN to that address, port 7777.
3. Test from outside your network, for example a laptop on a phone hotspot: `nc -vz hearth.example.net 7777` should say "succeeded" or "open".

## Path B: a WireGuard tunnel through a VPS (carrier-grade NAT)

Rent the smallest VPS with a fixed public IPv4 address. It only forwards encrypted traffic, so it can't read posts. It does see what the network sees anyway: the plaintext handshake (pubkeys), when people connect, and roughly how much they transfer.

```
friend ──TCP 7777──▶ VPS 203.0.113.10 ══WireGuard══ VM 10.77.0.2 (Hearth)
                     DNAT, no MASQUERADE            replies go back up the tunnel
```

**Friends' real IP addresses must survive the tunnel.** Hearth allows 8 connections per IP address and 5 invite attempts per IP per 10 minutes. With MASQUERADE (the usual VPN recipe), everyone would arrive from the VPS's tunnel address. The whole group would share those limits, and one friend's typo-ed invite would lock everyone out. So the VPS only changes the destination (DNAT). The VM then has to send replies back through the tunnel, not out of its own router: that's the policy routing below.

On **both** machines, install WireGuard and make that machine's key pair (private key readable by root only):
```bash
sudo apt install -y wireguard
sudo sh -c 'cd /etc/wireguard && umask 077 && wg genkey | tee private.key | wg pubkey > public.key'
```

**On the VPS**, `/etc/wireguard/wg0.conf`. Replace `eth0` with the VPS's public interface; `ip route show default` names it.
```ini
[Interface]
Address = 10.77.0.1/24
ListenPort = 51820
PrivateKey = <the VPS's private.key>
# Friends' connections to 7777 go down the tunnel, keeping their source IP (DNAT, no MASQUERADE).
PostUp = sysctl -w net.ipv4.ip_forward=1
PostUp = iptables -t nat -A PREROUTING -i eth0 -p tcp --dport 7777 -j DNAT --to-destination 10.77.0.2
PostUp = iptables -I FORWARD -i eth0 -o wg0 -p tcp -d 10.77.0.2 --dport 7777 -j ACCEPT
PostUp = iptables -I FORWARD -i wg0 -o eth0 -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
PostDown = iptables -t nat -D PREROUTING -i eth0 -p tcp --dport 7777 -j DNAT --to-destination 10.77.0.2
PostDown = iptables -D FORWARD -i eth0 -o wg0 -p tcp -d 10.77.0.2 --dport 7777 -j ACCEPT
PostDown = iptables -D FORWARD -i wg0 -o eth0 -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT

[Peer]
# The home VM
PublicKey = <the VM's public.key>
AllowedIPs = 10.77.0.2/32
```
The VPS's firewall must allow UDP 51820 (the tunnel) and TCP 7777 (friends) in.

**On the VM**, `/etc/wireguard/wg0.conf`:
```ini
[Interface]
Address = 10.77.0.2/24
PrivateKey = <the VM's private.key>
# Don't route all of the VM's traffic into the tunnel; only Hearth's replies (the rule below).
Table = off
# Anything sent from 10.77.0.2 (a reply to a friend who came through the tunnel) goes back up it.
PostUp = ip route add default dev wg0 table 7777
PostUp = ip rule add from 10.77.0.2 table 7777
PostDown = ip rule del from 10.77.0.2 table 7777
PostDown = ip route del default dev wg0 table 7777

[Peer]
# The VPS
PublicKey = <the VPS's public.key>
Endpoint = 203.0.113.10:51820
# Any source: friends' packets arrive with their own IP addresses.
AllowedIPs = 0.0.0.0/0
# Keeps the home router's NAT open, so the VPS can always reach the VM.
PersistentKeepalive = 25
```
Start it on both machines, VPS first: `sudo systemctl enable --now wg-quick@wg0`. Point `hearth.example.net` at the VPS's IP. On the VM, `sudo journalctl -u hearth-home` should now show friends' own addresses in "Handshake complete with … (203.0.113.77:51234)", not 10.77.0.1.

## Path C: Tailscale Funnel (a VM on your tailnet only)

Funnel puts one port of a tailnet machine on the internet, under its Tailscale name. Friends' connections reach Tailscale's relay servers, cross your tailnet to the VM, and tailscaled hands them to Hearth on `127.0.0.1:7777`.

```
friend ──TLS :443──▶ Funnel relay ══tailnet══▶ VM: tailscaled ──PROXY v2 + Hearth──▶ 127.0.0.1:7777
                     routes by name             unwraps the TLS
```

Funnel has two rules, and here is how Hearth fits them:
- **TLS only, on port 443, 8443 or 10000.** A friend's node wraps its connection in TLS when the address starts with `tls://`, as their invite link will. Hearth's own handshake still runs inside and still checks the home node's key from the link. TLS is opened on the VM, not on Tailscale's servers, which see only encrypted bytes: who connects to the name, when, and roughly how much.
- **Everyone arrives from 127.0.0.1.** The whole group would then share the per-IP limits, which are 8 connections per IP and 5 invite attempts per IP per 10 minutes. The ninth friend online would be refused, and one friend's typo-ed invite would lock everyone out. So tailscaled starts each connection with a PROXY v2 header naming the friend's own address (`--proxy-protocol=2`), and Hearth reads it (`run --proxy-protocol`). Hearth believes that header blindly, so it only accepts it on a loopback address.

**1. Allow Funnel on your tailnet.** In the Tailscale admin console:
- under DNS, turn on MagicDNS and HTTPS certificates;
- under Access controls, the policy file needs the `funnel` attribute for the VM. The default policy grants it to every member:
```json
"nodeAttrs": [{"target": ["autogroup:member"], "attr": ["funnel"]}]
```
On the VM, `tailscale funnel --help` must list `--tls-terminated-tcp` and `--proxy-protocol`. If it doesn't, update Tailscale. To check that the policy reached the VM:
```bash
tailscale status --json | python3 -c 'import json,sys; me=json.load(sys.stdin)["Self"]; print("name:", me.get("DNSName")); print("tags:", me.get("Tags")); print("caps:", [c for c in (me.get("CapMap") or {}) if "funnel" in c or c == "https"])'
```
`caps` must list `https`, `funnel` and a `funnel-ports` entry. Without `https`, turn on HTTPS certificates. Without `funnel`:
- the VM may be tagged: `autogroup:member` doesn't cover tagged machines, so add the tag to the target;
- or you edited another tailnet's policy. If `tailscale funnel` says Funnel isn't enabled and its link answers "node not found", your browser is on a different tailnet from the VM. The DNS page's tailnet name must match the end of the VM's `name`.

**2. Make Hearth listen on loopback only, reading PROXY headers.** Do this after install step 6:
```bash
sudo sed -i 's|--listen 0.0.0.0:7777|--listen 127.0.0.1:7777 --proxy-protocol|' /etc/systemd/system/hearth-home.service
sudo systemctl daemon-reload
sudo systemctl restart hearth-home
sudo journalctl -u hearth-home -n 20
```
The log should show `Listening on 127.0.0.1:7777`, and the banner says caller addresses come from the PROXY header. Redo the `sed` after copying a new unit file in an update.

**3. Open the funnel.**
```bash
sudo tailscale funnel --bg --proxy-protocol=2 --tls-terminated-tcp=443 tcp://127.0.0.1:7777
tailscale funnel status
```
`status` shows the public address, like `hearth-vm.tail1234.ts.net:443`. With `--bg`, the funnel runs in the background until you turn it off, and Tailscale resumes it after a reboot. Check that after the reboot test. A brand-new name can take up to 10 minutes to appear in public DNS.

**4. Invite with that name:** `hearth invite --data-dir /var/lib/hearth --host tls://hearth-vm.tail1234.ts.net:443`. Leave out the `tls://` and `join` fails, either with "the other side closed the connection" or by saying the address needs `tls://`.

**5. Test from a machine that is not on your tailnet**, for example a laptop on a phone hotspot with Tailscale off:
- `Test-NetConnection hearth-vm.tail1234.ts.net -Port 443` (Windows) or `nc -vz hearth-vm.tail1234.ts.net 443` should succeed;
- then a real `join`. On the VM, `sudo journalctl -u hearth-home` should show the friend's own address in "Handshake complete with … (198.51.100.23:51234)", not 127.0.0.1.

Things to know:
- Funnel is labelled beta, and its traffic is subject to bandwidth limits Tailscale doesn't publish. Text posts are tiny; if photos crawl, path B is the fallback.
- The `ts.net` name is public: certificate transparency logs list it, so strangers can find it and knock. Without an invite they get nothing, and the per-IP limits apply to them too.
  - On the real VM, web crawlers arrived within minutes. Each one logs an INFO line, `a web request, not Hearth (probably a crawler that found this address; harmless)`, and the connection is dropped after its first 4 bytes. They stay out of the `grep` for WARNING and ERROR.
- To close it, repeat the command with its flags and `off` at the end: `sudo tailscale funnel --proxy-protocol=2 --tls-terminated-tcp=443 tcp://127.0.0.1:7777 off`. `sudo tailscale funnel reset` clears every serve and funnel setting on the VM.

### Path C in a VM of its own

Use this when the machine you can use sits on someone else's tailnet, or when Hearth should be walled off from that machine. Hearth gets its own VM, and the VM joins **your** tailnet and runs Funnel itself. The other tailnet's admin has nothing to change, and their tailnet's name never becomes public. Tested with Multipass; other hypervisors work the same way.

**1. Your tailnet's policy, before the VM joins.** A tag keeps the VM, which faces the internet, from opening connections to your other devices, while they can still reach it. Change the allow-all grant's `src` from `*` to `autogroup:member`, and add the tag:
```json
"tagOwners": {"tag:hearth": ["autogroup:admin"]},
"grants": [{"src": ["autogroup:member"], "dst": ["*"], "ip": ["*"]}],
"nodeAttrs": [{"target": ["autogroup:member", "tag:hearth"], "attr": ["funnel"]}],
```
A Tailscale SSH rule for `autogroup:self` doesn't cover tagged machines: use `multipass shell` or plain SSH.

**2. The VM, on the host.**
```bash
multipass launch 24.04 --name hearth --cpus 1 --memory 2G --disk 30G
multipass shell hearth
```
- **Network:** add the host's network option (such as `--network`) to put it on its own VLAN. The VM needs outbound internet: HTTPS is enough, and outbound UDP 41641 makes connections more direct. Nothing inbound.
- **Name:** `hearth` becomes part of the public name in every invite link, so pick it now.
- **Disk:** the photo quota defaults to 20 GiB. On a smaller disk, lower `storage_quota_gib` in `config.json` to leave the system a few GiB.

**3. Tailscale in the VM.**
```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up --advertise-tags=tag:hearth
```
Open the login link in a browser signed in to your own tailnet. The check in step 1 above should then show `tags: ['tag:hearth']` and all three `caps`.

**4. The code.** Multipass installed as a snap can't see the host's `/tmp`, so copy the project archive into your home folder on the host first, then into the VM:
```bash
multipass transfer ~/hearth.tgz hearth:/tmp/
```
Then, inside the VM, run install steps 1 to 6 and path C steps 2 to 5, unpacking the archive to `/opt/hearth` in step 2.

To update later without GitHub, make a new archive and transfer it the same way. Then, in the VM: `sudo tar -xzf /tmp/hearth.tgz -C /opt/hearth && sudo /opt/hearth/.venv/bin/pip install /opt/hearth && sudo systemctl restart hearth-home`.

The firewall (install step 7) differs. Allow SSH with `sudo ufw allow OpenSSH` instead of from a LAN range: `multipass shell` itself logs in over SSH from the host, and Multipass VMs accept only key logins.

**5. Reboots.** Multipass starts a VM again if it was running when the host went down. After the host's first reboot, `multipass list` should show `hearth` as Running. If it doesn't, `multipass start hearth`.

## Backups

`backup` takes a consistent snapshot even while the service runs: SQLite's online backup API, plus the key files, `config.json` and every image chunk, in one `.tar`. Never copy `hearth.db` by hand while the service runs. It can catch the database mid-write and give you a torn copy.
```bash
sudo install -d -o hearth -g hearth -m 0700 /var/backups/hearth
hearth backup --data-dir /var/lib/hearth --out /var/backups/hearth/hearth-$(date +%F).tar
```
Nightly at 03:15: run `sudo crontab -u hearth -e` and add this line (cron needs `%` escaped):
```
15 3 * * * /opt/hearth/.venv/bin/python -m hearth backup --data-dir /var/lib/hearth --out /var/backups/hearth/hearth-$(date +\%F).tar
```
Copy the backups off the VM, for example with `rsync` from another machine. The archive is mode 0600 and holds the encrypted keys plus metadata: who posted when. It never holds the passphrase, which is why the passphrase lives in your password manager. Keep one backup offline as well: losing `identity.key` means losing the group's admin, for good. Backups grow with the photos. Prune old ones with `find /var/backups/hearth -name 'hearth-*.tar' -mtime +14 -delete` (add it to the same crontab).

**Restoring**, on this VM or a fresh one set up with steps 1, 2 and 6. On a fresh one, recreate `/etc/hearth/passphrase` from your password manager, as in step 4.
```bash
sudo systemctl stop hearth-home
sudo mv /var/lib/hearth /var/lib/hearth.before-restore
sudo install -d -o hearth -g hearth -m 0700 /var/lib/hearth
sudo -u hearth tar -xf /var/backups/hearth/hearth-2026-10-27.tar -C /var/lib/hearth
hearth debug verify --data-dir /var/lib/hearth
sudo systemctl start hearth-home
```
`debug verify` re-checks every signature, hash chain and image chunk. It should end with "All N entries verified" and "All M image chunks match their hashes".

## Running it

- **Health:** `hearth status --data-dir /var/lib/hearth` shows members, connected friends, entries per person, forks, waiting uploads, and storage against the quota.
- **Storage:** nothing is ever deleted, and each post can add up to 40 MiB of photos. When `status` gets near the quota, raise `storage_quota_gib` in `config.json` on purpose, then `sudo systemctl restart hearth-home`.
- **Updates:** `cd /opt/hearth && sudo git pull && sudo .venv/bin/pip install . && sudo systemctl restart hearth-home`.
- **If it won't start:** `sudo journalctl -u hearth-home -n 50`. A wrong passphrase file says "wrong passphrase" and systemd retries every 5 s until you fix it. If a different Python than this guide's refuses to run under `MemoryDenyWriteExecute=yes` (an mprotect or "Permission denied" error at start), delete that line from the unit, run `daemon-reload`, and note it here.
- **What the VM's disk gives away:** the passphrase file sits next to the data. Anyone with a full image of the VM's disk can read everything. Encrypt the disk at the hypervisor if you can.

## What has been tested, and how

Everything in this guide ran, as written, on 2026-10-27 in Ubuntu 24.04 containers (Python 3.12.3, systemd 255, a real WireGuard kernel module), standing in for the VM, the VPS and two laptops:
- Steps 1 to 8: the service starts under the full sandbox, `MemoryDenyWriteExecute` included. Argon2id `moderate` took 0.73 s per key file, and the start peaked at 282 MiB of RAM.
- A "reboot" (restarting the container) brought the service back with nobody logged in.
- `systemctl stop` is a clean stop: SIGTERM, then "Node stopped" within a second.
- Path B: both laptops joined through the VPS, and the VM's log showed their own addresses. A photo arrived byte for byte. A laptop offline for 26 posts caught up in one sync. Deleting the VM's `ip rule` broke new connections through the VPS, which shows the policy routing is needed.
- Backups, run by hand and as the cron line would run them, were taken while the service ran. The restore steps, `debug verify` and a restart all worked, and both laptops reconnected and synced. The passphrase appears nowhere in the archive.

- Path C, on 2026-10-29. Funnel itself needs a real tailnet, so Ubuntu 24.04's HAProxy stood in for tailscaled: TLS on port 443 with a self-signed certificate, then PROXY v2 to `127.0.0.1:7777`, with the unit changed by the `sed` above. Two friend containers without Tailscale joined with `tls://` links. The VM's log showed their own addresses, and a 300 KB photo arrived byte for byte. Port 7777 refused connections from outside the VM. After a reboot, both services came back and the friends reconnected. `systemd-analyze security` still rates it 1.5, and no WARNING or ERROR lines appeared anywhere.

- On real machines, on 2026-10-30, following "Path C in a VM of its own":
  - a Multipass VM (Ubuntu 24.04) on its own VLAN, joined to the owner's tailnet, with real Funnel;
  - a laptop on a phone hotspot, with Tailscale off, joined with a `tls://` link and synced, and the VM's log showed the hotspot's address;
  - crawlers found the name within minutes and were dropped;
  - after the host rebooted, the owner reported that the VM, the service and the funnel all came back.

What a container can't stand in for: your router's port forwarding, your ISP's NAT, a real day offline, and your VM's own Python and kernel. That's the next section.

## Is it done? (Phase 8's checks)

1. `systemctl status hearth-home` is active; after `sudo reboot` it's active again, with no login.
2. Two laptops on different networks, say home Wi-Fi and a phone hotspot, each `join` with their own invite, then `run`. A post with a photo from one shows on the other.
3. Close one laptop for a day, then `run` it again: `feed` catches up on everything posted meanwhile.
4. Take a backup while the service runs, restore it as above (on a spare VM, or into a test folder with `--data-dir`), and `debug verify` passes.
5. Path C only: after the reboot, `tailscale funnel status` still lists the funnel, and the VM's log shows friends' own addresses, not 127.0.0.1.
