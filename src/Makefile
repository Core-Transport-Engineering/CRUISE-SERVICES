all:	fxs
#NCS_PACKAGES=/opt/ncs/packages
NCS_PACKAGES=/var/opt/ncs/packages
YANGPATH = --yangpath yang --yangpath $(NCS_PACKAGES)/cisco-ios/src/ncsc-out/modules/yang --yangpath $(NCS_PACKAGES)/cisco-iosxr/src/ncsc-out/modules/yang --yangpath $(NCS_PACKAGES)/resource-manager/src/yang --yangpath $(NCS_PACKAGES)/id-allocator/src/yang --yangpath $(NCS_PACKAGES)/juniper-junos/src/ncsc-out/modules/yang
# Include standard NCS examples build definitions and rules
include $(NCS_DIR)/src/ncs/build/include.ncs.mk

SRC = $(wildcard yang/*.yang)
FXS = $(SRC:yang/%.yang=../load-dir/%.fxs)

fxs:	$(FXS)

../load-dir/%.fxs:	yang/%.yang
	$(NCSC)  `ls $*-ann.yang  > /dev/null 2>&1 && echo "-a $*-ann.yang"` \
             $(YANGPATH) -c -o $@ $<

clean:
	rm -rf  ../load-dir/*.fxs
