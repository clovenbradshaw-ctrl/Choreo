module.exports = {
  apps: [{
    name: 'choreo',
    script: 'choreo_runtime.py',
    interpreter: 'python3',
    args: '--port 8420 --dir /home/admin/choreo/instances',
    cwd: '/home/admin/choreo',
    env: {
      CHOREO_PORT: '8420',
      CHOREO_DIR: '/home/admin/choreo/instances'
    }
  }]
};
