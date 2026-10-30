# SSH git server for test_git_ssh_integration.py.
FROM alpine:3.24@sha256:be2656061b536c1883c4034790e21d0a09c5aaaf51968b89368a3c0371ea2c9a
RUN apk add --no-cache openssh git \
 && ssh-keygen -A \
 && adduser -D -h /home/git -s /usr/bin/git-shell git \
 && echo 'git:*' | chpasswd -e \
 && mkdir -p /home/git/.ssh \
 && printf 'PasswordAuthentication no\nPubkeyAuthentication yes\nPermitRootLogin no\n' >> /etc/ssh/sshd_config
EXPOSE 22
CMD ["/usr/sbin/sshd", "-D", "-e"]
