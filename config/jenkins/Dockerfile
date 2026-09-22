ARG JENKINS_IMAGE=jenkins/jenkins:2.568.3-lts-jdk21@sha256:c1e4c349365f6d16d88595b2c5f7e8ff39b8ae1d061f62420bac193b4b9616d0
FROM ${JENKINS_IMAGE}

COPY --chown=jenkins:jenkins plugins.txt /usr/share/jenkins/ref/plugins.txt
RUN jenkins-plugin-cli \
      --jenkins-update-center-download-url https://archives.jenkins.io/plugins \
      --plugin-file /usr/share/jenkins/ref/plugins.txt

COPY --chown=jenkins:jenkins casc.yaml /usr/share/jenkins/ref/casc.yaml
COPY --chown=jenkins:jenkins casc-local-realm.yaml /usr/share/jenkins/ref/casc-local-realm.yaml
COPY --chown=jenkins:jenkins jenkins-entrypoint.sh /usr/local/bin/devops-lab-jenkins
COPY validate-origin.sh /usr/local/bin/devops-lab-validate-origin
USER root
RUN chmod 0755 /usr/local/bin/devops-lab-jenkins /usr/local/bin/devops-lab-validate-origin
USER jenkins
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/devops-lab-jenkins"]
