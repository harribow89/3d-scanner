
  Matterport Pro2 board -> Xtion network sensor head  (Debian 12)
  ----------------------------------------------------------------
  Camera service : xtion-head  (tcp/5599)     systemctl status xtion-head
  Live check     : python3 /opt/xtion-head/head_probe.py 127.0.0.1
  Logs           : journalctl -u xtion-head -f
  Settings       : /boot/head-config.txt  (hostname, Wi-Fi, HEAD_ARGS)
                   /etc/default/xtion-head (default camera args)
                   /etc/xtion-head.json    (optional camera order:
                   {"cameras":[{"ns":"camera1","port":"1-1.2"},...]})
  Login          : scanner / scanner   (change it: passwd)
  Network        : DHCP on Ethernet / Wi-Fi, mDNS name <hostname>.local,
                   USB cable on the OTG port: board = 192.168.7.2

  On the PC (scanner project):
    SCANNER_HEAD=matterport-head.local ./run_scanner_docker.sh multi
    .venv/bin/python calibrate_head.py --head matterport-head.local

