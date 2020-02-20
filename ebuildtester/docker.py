import os, sys, re
import subprocess

import ebuildtester.options as options
from ebuildtester.utils import massage_string

class ExecuteFailure(Exception):
    pass

class Docker:

    def __init__(self, local_portage, overlay_dirs):
        """Create a new container."""

        docker_image = "gentoo/stage3-amd64"
        repo_names = self._get_repo_names(overlay_dirs)
        overlay_mountpoints = [os.path.join("/var/lib/overlays", r) for r in repo_names]

        self._setup_container(docker_image)
        self._create_container(docker_image, local_portage, zip(overlay_dirs, overlay_mountpoints))
        self._start_container()
        self._tweak_settings()
        self._enable_overlays(repo_names)
        self._enable_ccache()
        self._enable_test()
        self._mask()
        self._unmask_atom()
        self._unmask()
        self._update()
        self._install_basics()
        self._enable_global_use()
        self._set_gcc()
        self._print_summary()

    def execute(self, cmd):
        """Execute command in container.

        cmd is a string which is executed within a bash shell.
        """

        options.log.info("%s" % (cmd))
        docker_cmd = ["docker", "exec", "--interactive"]
        docker_cmd += self._set_env(options.exec_env)
        docker_cmd += self._set_env(options.sh_env)
        docker_cmd += [self.cid, "/bin/bash"]
        docker = subprocess.Popen(docker_cmd,
                                  stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE,
                                  stdin=subprocess.PIPE,
                                  universal_newlines=True)

        docker.stdin.write(cmd + "\n")
        docker.stdin.close()

        stdout_reader = os.fork()
        if stdout_reader == 0:
            try:
                self._reader(docker, docker.stdout, "stdout")
            except KeyboardInterrupt:
                pass
            sys.exit(0)

        stderr_reader = os.fork()
        if stderr_reader == 0:
            try:
                self._reader(docker, docker.stderr, "stderr")
            except KeyboardInterrupt:
                pass
            sys.exit(0)

        try:
            os.waitid(os.P_PID, stdout_reader, os.WEXITED)
            os.waitid(os.P_PID, stderr_reader, os.WEXITED)
            docker.wait()
        except KeyboardInterrupt:
            try:
                options.log.info("received keyboard interrupt")
                docker.terminate()
                self.shell()
                options.log.info("return from shell, initiating shutdown")
                self.cleanup()
                sys.exit(0)
            except OSError:
                pass
            docker.wait()

        if docker.returncode != 0:
            options.log.error("running in container %s" % (str(self.cid)))
            raise ExecuteFailure("failed command: \"%s\"" % (cmd))

    def shell(self):
        """Run an interactive shell in container."""

        options.log.info("running interactive shell in container ...")
        docker_cmd = ["docker", "exec", "--tty", "--interactive"]
        docker_cmd += self._set_env(options.sh_env)
        docker_cmd += [self.cid, "/bin/bash", "--login"]
        docker = subprocess.Popen(docker_cmd)

        try:
            docker.wait()
        except KeyboardInterrupt:
            options.log.info("ignoring keyboard interrupt")

    def cleanup(self):
        """Clean up."""

        if options.options.rm:
            self.remove()

    def remove(self):
        """Remove the docker container."""

        options.log.info("stopping container ...")
        docker = subprocess.Popen(["docker", "kill", self.cid[:12]])
        docker.wait()
        options.log.info("deleting container ...")
        docker = subprocess.Popen(["docker", "rm", self.cid[:12]])
        docker.wait()

    def _reader(self, proc, stream, name):
        """Read from a subprocess stream."""

        while True:
            out = stream.readline()
            if out == "" and proc.poll() is not None:
                break
            options.log.info("%s (%s): %s" %
                             (self.cid[:12], name, out.rstrip()))
            options.log_ch.flush()

    def _setup_container(self, docker_image):
        """Setup the container."""

        if options.options.pull:
            docker_args = ["docker", "pull", docker_image]
            docker = subprocess.Popen(docker_args)
            docker.wait()

    def _create_container(self, docker_image, local_portage, overlays):
        """Create new container."""

        docker_args = [
            "docker", "create",
            "--tty",
            "--cap-add", "SYS_ADMIN",
            "--device", "/dev/fuse",
            "--workdir", "/root",
            "--volume", "%s:/var/db/repos/gentoo" % local_portage,
            "--volume", "%s/distfiles:/var/cache/distfiles" % local_portage,
            "--volume", "%s/packages:/var/cache/binpkgs" % local_portage]

        for o in overlays:
            docker_args += ["--volume=%s:%s" % o]

        if options.options.ccache_dir is not None:
            docker_args += ["--volume=%s:/var/cache/ccache" % " ".join(options.options.ccache_dir)]

        docker_args += [docker_image]
        options.log.info("cmdline: %s" % " ".join(docker_args))
        docker = subprocess.Popen(docker_args, stdout=subprocess.PIPE)
        docker.wait()
        if docker.returncode != 0:
            raise Exception("failure creating docker container")

        lines = docker.stdout.readlines()
        if len(lines) > 1:
            raise Exception("more output than expected")
        self.cid = massage_string(lines[0]).strip()
        options.log.info("prepare container: %s" % (self.cid[:12]))

    def _start_container(self):
        """Start the container."""

        docker_args = ["docker", "start", "%s" % self.cid]
        docker = subprocess.Popen(docker_args, stdout=subprocess.PIPE)
        docker.wait()
        if docker.returncode != 0:
            raise Exception("failure creating docker container")

    def _set_env(self, env_list):
        """Set some env params form global shell."""

        docker_env_args = []
        for e in env_list:
            docker_env_args += ["--env", e]
        return docker_env_args

    def _tweak_settings(self):
        """Tweak settings."""

        if options.options.quick:
        	return

        options.log.info("tweaking settings")

        self.execute("sed -i -e \"/^#en_US.UTF-8/s/^#//\" /etc/locale.gen && locale-gen")
        self.execute("eselect locale set en_US.UTF-8")

        options.log.info("setting Gentoo profile to %s" % options.options.profile)
        self.execute("eselect profile set %s" % options.options.profile)

        # Disable the usersandbox feature, it's not working well inside a
        # docker container.
        self.execute(
            "echo MAKEOPTS=\\\"-j%d\\\"" % options.options.threads +
                " >> /etc/portage/make.conf && " +
            "echo FEATURES=\\\"-sandbox -usersandbox\\\" >> /etc/portage/make.conf && " +
            "echo EMERGE_DEFAULT_OPTS=\\\"--autounmask --autounmask-write --autounmask-continue --usepkg --oneshot\\\"" +
                " >> /etc/portage/make.conf && " +
            "echo CLEAN_DELAY=0 >> /etc/portage/make.conf"
        )

        if options.options.unstable:
            self.execute("echo ACCEPT_KEYWORDS=\\\"~amd64\\\" " +
                         ">> /etc/portage/make.conf")
        if options.options.with_X:
            self.execute("echo USE=\\\"X\\\" >> /etc/portage/make.conf")
        if options.options.with_vnc:
            self.execute("mkdir -p /etc/portage/package.use")
            self.execute("echo \"net-misc/tigervnc server\" >> /etc/portage/package.use/tigervnc")

        self.execute("env-update")

    def _get_repo_names(self, overlay_dirs):
        """Get repo names from local overlay settings."""

        repo_names = []
        for o in overlay_dirs:
            with open(os.path.join(o, "profiles/repo_name"), "r") as f:
                for repo_name in f:
                    repo_names.append(repo_name.replace("\n", ""))

        return repo_names

    def _enable_overlays(self, repo_names):
        """Enable overlays."""

        for r in repo_names:
            self.execute(
                "mkdir -p /etc/portage/repos.conf && " +
                "echo -e \"[%s]\\n" % str(r) +
                "location = /var/lib/overlays/%s\\n" % str(r) +
                "master = gentoo\" >> /etc/portage/repos.conf/overlays.conf"
            )

    def _enable_ccache(self):
        """Enable ccache."""

        if options.options.ccache_dir is not None:
            options.log.info("enabling ccache feature")
            self.execute(
	                "mkdir -p /var/cache/ccache /tmp/ccache-tmpfiles && " +
	                "chown root:portage /var/cache/ccache /tmp/ccache-tmpfiles && " +
	                "chmod 2775 /var/cache/ccache /tmp/ccache-tmpfiles && " +
	                "echo -e \"FEATURES=\\\"\${FEATURES} ccache\\\"\\n" +
	                "CCACHE_DIR=\\\"/var/cache/ccache\\\"\\n" +
	                "CCACHE_MAXSIZE=\\\"30G\\\"\\n" +
	                "CCACHE_UMASK=\\\"002\\\"\\n" +
	                "CCACHE_NLEVELS=\\\"3\\\"\\n" +
	                "CCACHE_COMPILERCHECK=\\\"%compiler% -v\\\"\\n" +
	                "CCACHE_COMPRESS=\\\"true\\\"\\n" +
	                "CCACHE_COMPRESSLEVEL=\\\"6\\\"\\n" +
	                "CCACHE_TEMPDIR=\\\"/tmp/ccache-tmpfiles\\\"\" >> /etc/portage/make.conf"
                )
        else:
            options.log.info("enabling ccache skipped, no directory specified")

    def _enable_test(self):
        """Enable test FEATURES for ATOM."""

        if options.options.atom is not None:
            options.log.info("enabling test feature for %s" %
                             options.options.atom)
            self.execute("mkdir -p /etc/portage/env")
            for a in options.options.atom:
                self.execute(
                    "echo \"%s tester.conf\" >> /etc/portage/package.env" % a)
            self.execute(
                "echo \"FEATURES=\\\"test splitdebug\\\"\" " +
                "> /etc/portage/env/tester.conf")
        else:
            options.log.info("enabling tests skipped, no atoms specified")

    def _mask(self):
        """Mask other atoms."""

        if options.options.mask:
            options.log.info("masking additional atoms")
            for a in options.options.mask:
                options.log.info("  masking %s" % a)
                self.execute(
                    "mkdir -p /etc/portage/package.mask && " +
                    "echo \"%s\" >> /etc/portage/package.mask/testbuild" % str(a)
                )

    def _unmask_atom(self):
        """Unmask the atom to install."""

        if options.options.atom is not None:
            options.log.info("unmasking %s" % options.options.atom)
            for a in options.options.atom:
                if options.options.live_ebuild:
                    unmask_keyword = "**"
                else:
                    unmask_keyword = "~amd64"
                self.execute(
                    "mkdir -p /etc/portage/package.{accept_keywords,unmask} && " +
                    "echo \"%s\" >> /etc/portage/package.unmask/testbuild && " % str(a) +
                    "echo \"" + str(a) + "\" " + unmask_keyword + " >> " +
                        "/etc/portage/package.accept_keywords/testbuild"
                )

            if len(options.options.use) > 0:
                for a in options.options.atom:
                    self.execute("echo %s %s >> /etc/portage/package.use/testbuild" %
                        (str(a), " ".join(options.options.use)))
        else:
            options.log.info("no atoms to unmask")

    def _unmask(self):
        """Unmask other atoms."""

        if options.options.unmask:
            options.log.info("unmasking additional atoms")
            for a in options.options.unmask:
                options.log.info("  unmasking %s" % a)
                self.execute(
                    "mkdir -p /etc/portage/package.{accept_keywords,unmask} && " +
                    "echo \"%s\" >> /etc/portage/package.unmask/testbuild && " % str(a) +
                    "echo \"%s\" ~amd64 >> /etc/portage/package.accept_keywords/testbuild" % str(a)
                )

    def _update(self):
        """Update container."""

        if not options.options.update:
            options.log.info("skipping update")
        else:
            options.log.info("updating container")
            update_options = ["--verbose", "--update",
                              "--deep", "--newuse", "--changed-deps"]
            self.execute("emerge " + " ".join(update_options) + " @world")

    def _install_basics(self):
        """Install some basic packages."""

        if options.options.quick:
            return

        options.log.info("installing basic packages: %s" %
                         options.base_packages)
        self.execute("emerge --verbose %s" %
                     " ".join(map(str, options.base_packages)))

    def _enable_global_use(self):
        """Enable global USE settings."""
        if not options.options.global_use:
            options.log.info("no global USE flags given, skipping")
        else:
            options.log.info("setting global USE flags")
            for u in options.options.global_use:
                self.execute("euse --enable %s" % u)

    def _set_gcc(self):
        """Set gcc in the container."""

        if options.options.gcc_version:
            options.log.info("setting gcc")
            self.execute("mkdir -p /etc/portage/package.accept_keywords")
            self.execute(
                ("echo =sys-devel/gcc-%s ** >> " %
                 options.options.gcc_version) +
                "/etc/portage/package.accept_keywords/testbuild")
            self.execute("emerge --verbose sys-devel/gcc")
            gcc = re.sub("-r[0-9]+$", "", options.options.gcc_version)
            self.execute("gcc-config $(gcc-config --list-profiles | " +
                         ("grep %s | " % gcc) +
                         "sed -e 's:^.*\\[\\([0-9]\\+\\)\\].*:\\1:')")
            self.execute("emerge --verbose --oneshot sys-devel/libtool")

    def _print_summary(self):
        """Print summary."""

        if options.options.quick:
            return

        options.log.info("summary")
        self.execute(
            "if [[ -d /etc/portage/package.accept_keywords ]]; then " +
            "cat /etc/portage/package.accept_keywords/*; fi")
        self.execute("if [[ -f /etc/portage/package.use/testbuild ]]; then " +
                     "cat /etc/portage/package.use/testbuild; fi")
        self.execute("emerge --info")
        self.execute("qlop")
