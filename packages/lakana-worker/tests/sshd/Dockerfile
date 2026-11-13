# SSH git server for test_git_ssh_integration.py.
FROM alpine:3.24@sha256:f59abe656b692d901e184be3445e084786602108599d2a51d229c0f5a48c71bf
RUN apk add --no-cache openssh git \
 && ssh-keygen -A \
 && adduser -D -h /home/git -s /usr/bin/git-shell git \
 && echo 'git:*' | chpasswd -e \
 && mkdir -p /home/git/.ssh \
 && printf 'PasswordAuthentication no\nPubkeyAuthentication yes\nPermitRootLogin no\n' >> /etc/ssh/sshd_config
EXPOSE 22
CMD ["/usr/sbin/sshd", "-D", "-e"]
