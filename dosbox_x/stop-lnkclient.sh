#!/bin/sh
# Control directory for trigger files; override with PYTERLINK_CONTROL.
SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
CONTROL="${PYTERLINK_CONTROL:-$HOME/.control}"
UNAME=`whoami`
PS_LINE=`ps -ef |grep "dosbox -conf $SCRIPT_DIR/lnkclient-msdos.conf" | grep -v grep | head -n 1`
# we are killing specifically this task.
# UID is first arg, next is PID.
echo "PS_LINE: ${PS_LINE}"
CHECK_UNAME=`echo ${PS_LINE} | awk '{print $1}'`
GET_PID=`echo ${PS_LINE} | awk '{print $2}'`
#if [ "$UNAME" = "$CHECK_UNAME}" ] ; then
#{
#	echo "Username matches."
#}	
#else
#{
#	echo "Username does not match. Cannot proceed."
#	exit 2
#}
#fi
echo "Killing $GET_PID"
sudo /usr/bin/kill -TERM ${GET_PID}
PS_LINE=`ps -ef |grep "dosbox -conf $SCRIPT_DIR/lnkclient-msdos.conf" | grep -v grep | head -n 1`
echo "Process running: ${PS_LINE}."
rm -f "$CONTROL"/dosbox-start-lnkclient.txt 2>> /dev/null
rm -f "$CONTROL"/dosbox-run-lnkclient.txt 2>> /dev/null
rm -f "$CONTROL"/dosbox-stop-lnkclient.txt 2>> /dev/null
sudo /usr/bin/rm -f "$CONTROL"/dosbox-run-lnkclient.txt
sudo /usr/bin/rm -f "$CONTROL"/dosbox-start-lnkclient.txt
sudo /usr/bin/rm -f "$CONTROL"/dosbox-stop-lnkclient.txt

