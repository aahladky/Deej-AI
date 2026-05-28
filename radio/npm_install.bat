@echo off
cd /d "C:\Users\aahla\Downloads\SpotifySkipTracker-master\SpotifySkipTracker-master"
"C:\Program Files\nodejs\npm.cmd" install --legacy-peer-deps --ignore-scripts
echo npm install exit: %ERRORLEVEL%
echo Running electron postinstall...
"C:\Program Files\nodejs\node.exe" node_modules\electron\install.js
echo electron install exit: %ERRORLEVEL%
echo Running esbuild postinstall...
"C:\Program Files\nodejs\node.exe" node_modules\esbuild\install.js
echo esbuild install exit: %ERRORLEVEL%
