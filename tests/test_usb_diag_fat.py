# SPDX-FileCopyrightText: 2026 ARMHPC contributors
# SPDX-License-Identifier: GPL-2.0-only OR GPL-3.0-only
import hashlib
import importlib.util
from pathlib import Path
import struct
import pytest

source=Path(__file__).parents[1]/'tools/usb_diag_fat.py'
if not source.is_file():source=Path(__file__).with_name('usb_diag_fat.py')
spec=importlib.util.spec_from_file_location('usb_diag_fat',source)
fat=importlib.util.module_from_spec(spec);spec.loader.exec_module(fat)


class Volume:
    total=131072
    data_sector=2080
    def __init__(self):
        self.data=bytearray((self.data_sector+100)*512);self.calls=[]
        boot=self.data
        struct.pack_into('<H',boot,11,512);boot[13]=1
        struct.pack_into('<H',boot,14,32);boot[16]=1
        struct.pack_into('<I',boot,32,self.total);struct.pack_into('<I',boot,36,2048)
        struct.pack_into('<I',boot,44,2);boot[510:512]=b'\x55\xaa'
        self.link(0,0x0ffffff8);self.link(1,0x0fffffff);self.link(2,0x0fffffff)
    def link(self,cluster,next):struct.pack_into('<I',self.data,32*512+cluster*4,next)
    def put(self,cluster,data):
        offset=(self.data_sector+cluster-2)*512
        self.data[offset:offset+len(data)]=data
    def read(self,offset,length):
        assert offset%512==0 and length%512==0 and length<=1024**2
        assert offset+length<=self.total*512
        self.calls.append((offset,length));return bytes(self.data[offset:offset+length])
    def mount(self):return fat.FAT32ReadOnly(self.read,0,self.total*512)


def directory_entry(name,cluster,size,attr=32):
    row=bytearray(32);row[:11]=name
    row[11]=attr;struct.pack_into('<H',row,20,cluster>>16);struct.pack_into('<H',row,26,cluster&65535)
    struct.pack_into('<I',row,28,size);return bytes(row)


def long_name_entries(name,short):
    raw=name.encode('utf-16le')+b'\0\0'
    count=(len(raw)+25)//26;raw=raw.ljust(count*26,b'\xff')
    result=[]
    for index in reversed(range(1,count+1)):
        row=bytearray(32);row[0]=index|(64 if index==count else 0);row[11]=15
        row[13]=fat.short_checksum(short);part=raw[(index-1)*26:index*26]
        row[1:11]=part[:10];row[14:26]=part[10:22];row[28:32]=part[22:]
        result.append(bytes(row))
    return b''.join(result)


def test_directory_size_and_fragmented_chain_define_the_capture(tmp_path):
    disk=Volume();payload=b'a'*512+b'b'*512+b'c'*471
    disk.put(2,directory_entry(b'BOOT    SCR',4,len(payload)))
    disk.link(4,9);disk.link(9,6);disk.link(6,0x0fffffff)
    disk.put(4,payload[:512]);disk.put(9,payload[512:1024]);disk.put(6,payload[1024:])
    target=tmp_path/'boot.scr';result=disk.mount().copy_file('boot.scr',target)
    assert target.read_bytes()==payload
    assert result['directory_bytes']==result['bytes']==len(payload)
    assert result['sha256']==hashlib.sha256(payload).hexdigest()


def test_long_filename_in_nested_directories_is_matched_by_checksum(tmp_path):
    disk=Volume();name='meson-g12b-s922x-armhpc-oes1plus-v22.dtb';short=b'MESON-~1DTB'
    disk.put(2,directory_entry(b'DTB        ',3,0,16));disk.link(3,0x0fffffff)
    disk.put(3,directory_entry(b'AMLOGIC    ',4,0,16));disk.link(4,0x0fffffff)
    disk.put(4,long_name_entries(name,short)+directory_entry(short,5,4));disk.link(5,0x0fffffff)
    disk.put(5,b'\xd0\x0d\xfe\xed')
    volume=disk.mount();target=tmp_path/'dtb'
    assert volume.copy_file('dtb/amlogic/'+name,target)['bytes']==4
    assert target.read_bytes()==b'\xd0\x0d\xfe\xed'


def test_cycle_in_a_file_chain_does_not_duplicate_bytes_into_success(tmp_path):
    disk=Volume();disk.put(2,directory_entry(b'IMAGE      ',4,1024));disk.link(4,4)
    with pytest.raises(ValueError,match='Cycle'):
        disk.mount().copy_file('Image',tmp_path/'Image')
    assert not (tmp_path/'Image').exists()


def test_chain_shorter_than_directory_size_is_rejected(tmp_path):
    disk=Volume();disk.put(2,directory_entry(b'IMAGE      ',4,1024));disk.link(4,0x0fffffff)
    with pytest.raises(ValueError,match='shorter'):
        disk.mount().copy_file('Image',tmp_path/'Image')


def test_invalid_geometry_cannot_read_outside_boot_partition():
    disk=Volume();struct.pack_into('<I',disk.data,32,disk.total+1)
    with pytest.raises(ValueError,match='volume'):
        disk.mount()
    assert disk.calls==[(0,512)]


def test_ambiguous_casefolded_names_are_rejected():
    disk=Volume();row=directory_entry(b'BOOT    SCR',4,4)
    disk.put(2,row+row);disk.link(4,0x0fffffff)
    with pytest.raises(ValueError,match='ambiguous'):disk.mount().lookup('boot.scr')
