// ==============================================================================
// Flutter Build Pipeline — Jenkinsfile
// ==============================================================================
// This file lives in the Flutter project repo (not the bot repo).
// Configure a Jenkins Pipeline job to point at this file.
//
// Parameters (set automatically by the Telegram bot):
//   BRANCH           — Branch or commit hash to build (default: main)
//   BOT_CALLBACK_URL — Bot webhook URL for build result notification
//                      (empty for manual Jenkins triggers)

pipeline {
    agent { label 'flutter' }

    parameters {
        string(
            name: 'BRANCH',
            defaultValue: 'main',
            description: 'Branch or commit hash to build'
        )
        string(
            name: 'BOT_CALLBACK_URL',
            defaultValue: '',
            description: 'Bot webhook URL (set automatically by bot, leave empty for manual builds)'
        )
    }

    stages {
        stage('Checkout') {
            steps {
                checkout([
                    $class: 'GitSCM',
                    branches: [[name: "*/${params.BRANCH}"]],
                    userRemoteConfigs: [[
                        url: env.REPO_URL,
                        credentialsId: 'gitlab-credentials'
                    ]]
                ])
            }
        }

        stage('Build APK') {
            steps {
                sh 'flutter build apk --release'
            }
        }
    }

    post {
        success {
            script {
                if (params.BOT_CALLBACK_URL) {
                    def commitHash = sh(
                        script: 'git rev-parse HEAD',
                        returnStdout: true
                    ).trim()

                    sh """
                        curl -s -X POST "${params.BOT_CALLBACK_URL}" \
                            -F 'metadata={"queue_id": ${env.BUILD_NUMBER}, "status": "success", "commit_hash": "${commitHash}"};type=application/json' \
                            -F 'artifact=@build/app/outputs/flutter-apk/app-release.apk'
                    """
                }
            }
        }

        failure {
            script {
                if (params.BOT_CALLBACK_URL) {
                    def commitHash = sh(
                        script: 'git rev-parse HEAD || echo unknown',
                        returnStdout: true
                    ).trim()

                    sh """
                        curl -s -X POST "${params.BOT_CALLBACK_URL}" \
                            -F 'metadata={"queue_id": ${env.BUILD_NUMBER}, "status": "failed", "commit_hash": "${commitHash}", "logs": "Check Jenkins console for details"};type=application/json'
                    """
                }
            }
        }

        always {
            cleanWs()
        }
    }
}
