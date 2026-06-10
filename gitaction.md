You make changes in your IDE
           │
           ▼
    git add .
    git commit -m "your message"
    git push origin main
           │
           ▼
   GitHub receives the push
           │
           ▼
  GitHub Actions automatically triggers
           │
           ▼
  ✅ Job 1: VALIDATE
  - installs dependencies
  - checks Python syntax
  - verifies imports
  - if this FAILS → stops here, nothing deploys
           │
           ▼
  ✅ Job 2: BUILD & PUSH code
  - builds your Docker image
  - pushes it to Docker Hub
  - if this FAILS → stops here, EC2 not touched
           │
           ▼
  ✅ Job 3: DEPLOY
  - SSHs into your EC2 (3.105.157.60)d
  - pulls the new Docker image
  - stops the old running container
  - starts the new container with your changes
  - cleans up old images
           │
           ▼
  🚀 Your trading bot is live with new changes2ww
 
     