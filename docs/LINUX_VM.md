# User-prepared Linux guest

AgentStack accepts a **directly reachable Ubuntu 24.04 VM** and installs its
own runtime inside it. Native guest architectures are **ARM64** (`aarch64`) and
**amd64** (`x86_64`). The user owns the physical host, VM creation, hypervisor,
virtual disks, host networking, mesh enrollment and access tunnels. AgentStack
has no libvirt provider, VM module, hypervisor bootstrap or tunnel helper.

A guest does not need `/dev/kvm`, libvirt, a particular physical-host OS, or an
AgentStack cloud-init loader marker. Architecture checks inspect the guest only;
they cannot prove whether the user's hypervisor uses native virtualization.

## Prepare the VM independently

These are **operator examples**, not commands AgentStack runs. A separately
authorized agent can adapt them after inspecting the identified host and
reviewing its existing virtualization/network configuration. Do not change
Wi-Fi, firewall, VPN, bridges, routes or account grants without approval for
that host. On a Pi, use an ARM64 guest; on an x86 PC, use amd64. An x86 guest on
ARM uses emulation and is not native ARM qualification.

1. Prepare Ubuntu Server 24.04 using your preferred hypervisor or installer.
   [Canonical's cloud images](https://cloud-images.ubuntu.com/releases/noble/release/)
   include `ubuntu-24.04-server-cloudimg-arm64.img` and the matching `amd64.img`.
   Verify signed SHA256SUMS with a trusted Canonical key before using an image.
   A Pi bare-metal image is not the generic VM cloud image.
2. For an existing Ubuntu/libvirt host, review native KVM support and resources
   yourself. Typical operator-installed packages are `libvirt-daemon-system`,
   `libvirt-clients`, `virtinst`, `qemu-utils`, and `qemu-system-arm` plus
   `qemu-efi-aarch64` on ARM, or `qemu-system-x86` plus `ovmf` on amd64. Installing
   packages can enable daemons/networks; group membership is a separate privilege
   grant. AgentStack neither installs them nor creates a virtual network.
3. Supply an existing network in the hypervisor. Wi-Fi commonly uses NAT;
   bridging a Wi-Fi station is not equivalent to bridging Ethernet. If the VM
   has only a private NAT address, arrange a user-owned SSH forward or guest
   mesh identity so the Terraform computer can reach its SSH endpoint. A host's
   mesh identity does not automatically give the guest a mesh identity or LAN
   broadcast behavior. See [libvirt networking](https://wiki.libvirt.org/Networking).
4. Size the guest for selected services. The preflight minimum of 2 GiB RAM and
   2 GiB free storage is only an installation guard, not a full-stack capacity
   guarantee. For a 16 GB host, an illustrative guest allocation is 8 GiB RAM,
   4 vCPUs and at least a 40 GiB system disk; retain headroom for the host and
   size persistent data/backups separately. Qualify the actual workload.

For example, after the operator has prepared a verified cloud image, an existing
libvirt network and a cloud-init file containing the **existing** guest login
public key, these commands create a new native ARM VM. Replace every example
path/name and confirm the destination disk does not already exist:

```bash
qemu-img create -f qcow2 -F qcow2 \
  -b /absolute/images/ubuntu-24.04-server-cloudimg-arm64.img \
  /absolute/vms/agent-stack.qcow2 40G
virt-install --connect qemu:///system --name agent-stack \
  --virt-type kvm --arch aarch64 --machine virt --cpu host-passthrough \
  --memory 8192 --vcpus 4 --os-variant ubuntu24.04 --boot uefi \
  --disk path=/absolute/vms/agent-stack.qcow2,bus=virtio \
  --network network=YOUR_EXISTING_NETWORK,model=virtio \
  --cloud-init user-data=/absolute/config/guest-user-data.yaml \
  --import --graphics none --noautoconsole
```

For native amd64, choose the amd64 image, `--arch x86_64 --machine q35` and its
native firmware. Use `virt-install --osinfo list` to check your installed OS
catalog; update it independently if Ubuntu 24.04 is absent. The
[Ubuntu virt-install manual](https://manpages.ubuntu.com/manpages/noble/man1/virt-install.1.html)
describes these options. This recipe has not been boot-tested here.

A minimal operator-owned cloud-init example is:

```yaml
#cloud-config
users:
  - name: operator
    shell: /bin/bash
    sudo: ALL=(ALL) NOPASSWD:ALL
    lock_passwd: true
    ssh_authorized_keys:
      - REPLACE_WITH_EXISTING_PUBLIC_KEY
ssh_pwauth: false
disable_root: true
packages: [python3, ca-certificates, curl, jq]
```

This grants administration **inside the guest**; review it as part of your own
VM setup. Do not use placeholder keys, create credentials without authorization,
or copy a physical host's private keys/browser profile into the VM. Obtain the
guest's SSH public host key through a trusted console/provisioning channel.
An unauthenticated `ssh-keyscan` alone is not identity proof.

## Supply the reachable guest to AgentStack

Use a separate checkout/state for a new existing-guest deployment; never flip a
live cloud state to `existing`. `variables.tf` is the schema/default source.
Essential inputs are:

```hcl
cloud_provider              = "existing"
admin_ssh_host_override     = "vm.example.internal"
admin_username              = "operator"
existing_guest_ssh_port     = 22
existing_guest_architecture = "arm64" # or amd64; auto is the default
repo_ssh_private_key_path   = "/absolute/path/to/existing_guest_key"
ssh_public_key             = "REPLACE_WITH_EXISTING_AUTHORIZED_PUBLIC_KEY"
existing_guest_host_key    = "REPLACE_WITH_VERIFIED_GUEST_HOST_PUBLIC_KEY"
tailscale_enabled          = false
vpn_enabled                = false
```

Choose services/model settings normally and configure their authentication only
when authorized. The two key values above are public keys, not passwords or
private-key contents. AgentStack never creates a login key for this target.
The reachable endpoint may be a LAN/mesh DNS/IP or a user-owned local SSH forward
such as `127.0.0.1:2200`. IPv4/DNS transport is supported initially. Existing
user SSH proxy configuration is not imported into Terraform's connection: make
the supplied address and port directly reachable first.

`terraform plan` performs a read-only probe using the supplied private-key path
and **pinned guest public host identity**, checks Ubuntu 24.04 and the native
guest architecture, and validates `/opt/agent-stack/data`. It does not inspect
or contact a hypervisor. Unsupported OS/CPU, symlinks, non-directory paths and
unmarked nonempty data fail before runtime provisioning. SSH/probe failures
cannot authorize changing the user's access path. Plan/review the runtime
apply separately; each target needs its own authorization.

A normal directory on the guest filesystem is supported and created if missing.
For separately mounted persistent storage, the **user** formats/mounts it and
sets its fstab outside AgentStack, then sets
`existing_guest_require_data_mount = true`. Missing mounts fail closed. No
physical or virtual block device is formatted, mounted or unmounted by this
runtime target. Reusing nonempty unmarked data requires reviewing/backing it
up and explicitly setting `existing_guest_allow_existing_data = true`.
Previously managed data and identities are preserved on repeat application.

The common installer runs with sudo **inside the guest**: it installs Docker if
needed, configures selected services and runtime units, and uses the existing
workspace/Paperclip bundle. Cloud-init is waited for only when present;
no AgentStack loader marker or VM/data UUID output is required. Generic verified
SSH config can be generated for the supplied guest endpoint; it contains no
ProxyJump, hypervisor identity or tunnel configuration. `bin/agent-stack-ssh`
uses that config and its supplied port. [Docker's Ubuntu support](https://docs.docker.com/engine/install/ubuntu/)
includes Ubuntu 24.04 and amd64/ARM64; selected application images/tools still
need native runtime qualification.

## User-owned access and browser tunnels

If a remote computer cannot reach the guest's NAT address, the user or a
separately authorized agent can run this on the Terraform computer, after
verifying the hypervisor's host identity and existing SSH access:

```bash
ssh -NT -o StrictHostKeyChecking=yes -o ForwardAgent=no \
  -o ExitOnForwardFailure=yes \
  -L 127.0.0.1:2200:VM_PRIVATE_IP:22 HOST_USER@HYPERVISOR
```

Keep it running and give AgentStack `admin_ssh_host_override = "127.0.0.1"`
and `existing_guest_ssh_port = 2200`, with the **guest's** verified public host
key. AgentStack does not open, persist or reconnect this forward. Ctrl-C closes
it. The hypervisor must already be able to reach the guest. Do not expose the
local forward on `0.0.0.0` or change host routes/firewalls to force access.

For Paperclip, the user runs a separate forward **on the computer where the
browser actually runs**, using its own existing guest-authorized key/config:

```bash
ssh -F /path/to/reviewed/guest-ssh-config -NT \
  -o StrictHostKeyChecking=yes -o ForwardAgent=no \
  -o ExitOnForwardFailure=yes \
  -L 127.0.0.1:3100:127.0.0.1:3100 GUEST_ALIAS
```

That config targets the reachable guest endpoint; any proxy or preliminary
forward is user-owned. On the same computer as the runtime checkout, its
generated `.ssh/config` and `agent-stack` alias can supply the guest identity.
Do not copy controller-specific identity paths or private keys blindly to a Pi.
Open `http://localhost:3100`. Paperclip stays published on guest loopback, its
login/first-owner claim remains required, and bootstrap port 3101 is not exposed.
A remote/sandboxed browser must have an explicitly usable path to that computer's
localhost. Host browser tools, sessions and credentials are not inherited by
containers. See [Paperclip setup](PAPERCLIP.md).

Workspace SSH is a separate published port (default 2222), with its own container
host key. An SSH forward for guest port 22 does not expose workspace port 2222;
prepare a separate access path and `workspace_ssh_host_override` when required.

## Persistence and recovery

Back up `/opt/agent-stack/data`, private application state and protected Terraform
state before upgrades. AgentStack does not create/snapshot/destroy the VM or retain a removed
hypervisor module's volumes. Destroying this root's Terraform resources does not
remove the external VM or its guest data; verify a destroy plan before executing.
User-owned guest storage and backup policy determine resilience to VM loss.

Runtime updates use the normal checksum-triggered installer. Mutable image
versions/startup pulls limit reproducible rollback; qualify pinned images and
schema/data restoration separately. No automatic guest reimage or host recovery
is provided.

## Runtime qualification

Verify initial installation, repeat application, guest identity pinning and retained data on disposable native Ubuntu 24.04 ARM64 and amd64 guests. Verify workspace SSH and the authenticated Paperclip dashboard through the owner-provided access path. The offline fixtures do not exercise a live deployment.
