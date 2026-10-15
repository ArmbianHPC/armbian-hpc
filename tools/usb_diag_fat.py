# SPDX-FileCopyrightText: 2026 ARMHPC contributors
# SPDX-License-Identifier: GPL-2.0-only OR GPL-3.0-only
"""Read fixed BOOT files through bounded sector reads and FAT32 directory sizes."""
import hashlib
import os
import struct

BLOCK=1024**2


def need(value,message):
    if not value:raise ValueError(message)


def short_checksum(name):
    value=0
    for byte in name:value=(((value&1)<<7)+(value>>1)+byte)&255
    return value


class FAT32ReadOnly:
    def __init__(self,read_at,start,partition_bytes):
        need(start>=0 and start%512==0 and partition_bytes>0,'Invalid FAT32 partition range')
        self.read_at=read_at;self.start=start;self.limit=partition_bytes
        boot=self._read(0,512)
        need(boot[510:]==b'\x55\xaa','Invalid FAT32 boot signature')
        need(struct.unpack_from('<H',boot,11)[0]==512,'Only 512-byte FAT32 sectors are supported')
        self.spc=boot[13];self.reserved=struct.unpack_from('<H',boot,14)[0];self.fats=boot[16]
        need(self.spc in (1,2,4,8,16,32,64,128) and self.reserved>0 and self.fats in (1,2),'Invalid FAT32 geometry')
        need(struct.unpack_from('<H',boot,17)[0]==0 and struct.unpack_from('<H',boot,22)[0]==0,'Not a FAT32 volume')
        total=struct.unpack_from('<I',boot,32)[0];fat_sectors=struct.unpack_from('<I',boot,36)[0]
        flags=struct.unpack_from('<H',boot,40)[0];self.root=struct.unpack_from('<I',boot,44)[0]&0x0fffffff
        need(0<total*512<=partition_bytes and 0<fat_sectors*512<=8*BLOCK,'Invalid FAT32 volume/FAT size')
        self.limit=total*512
        self.data_sector=self.reserved+self.fats*fat_sectors
        need(self.data_sector<total,'FAT32 data region is outside the volume')
        self.clusters=(total-self.data_sector)//self.spc
        self.cluster_bytes=self.spc*512
        need(self.clusters>=65525 and (self.clusters+2)*4<=fat_sectors*512,'Invalid FAT32 cluster count')
        active=flags&15 if flags&128 else 0
        need(active<self.fats,'Invalid active FAT')
        self.fat=self._read((self.reserved+active*fat_sectors)*512,fat_sectors*512)
        self._cluster(self.root)

    def _read(self,offset,length):
        need(offset>=0 and offset%512==0 and length>0 and length%512==0 and offset+length<=self.limit,'FAT read exceeds its partition')
        chunks=[]
        for index in range(0,length,BLOCK):
            wanted=min(BLOCK,length-index)
            part=bytes(self.read_at(self.start+offset+index,wanted))
            need(len(part)==wanted,'Truncated FAT sector read')
            chunks.append(part)
        return b''.join(chunks)

    def _cluster(self,cluster):
        need(2<=cluster<self.clusters+2,'Invalid FAT cluster')
        return (self.data_sector+(cluster-2)*self.spc)*512

    def _chain(self,first):
        seen=set();cluster=first
        while True:
            self._cluster(cluster)
            need(cluster not in seen,'Cycle in FAT cluster chain')
            seen.add(cluster);yield cluster
            value=struct.unpack_from('<I',self.fat,cluster*4)[0]&0x0fffffff
            if value>=0x0ffffff8:return
            need(2<=value<self.clusters+2,'Truncated or invalid FAT cluster chain')
            cluster=value

    def _entries(self,first):
        long_parts={};long_total=0;long_sum=None
        for cluster_index,cluster in enumerate(self._chain(first)):
            need((cluster_index+1)*self.cluster_bytes<=BLOCK,'FAT directory exceeds the bounded size')
            data=self._read(self._cluster(cluster),self.cluster_bytes)
            for offset in range(0,len(data),32):
                entry=data[offset:offset+32]
                if entry[0]==0:return
                if entry[0]==0xe5:
                    long_parts={};long_total=0;long_sum=None;continue
                if entry[11]==15:
                    sequence=entry[0]&31
                    if entry[0]&64:long_parts={};long_total=sequence;long_sum=entry[13]
                    if 1<=sequence<=20 and entry[12]==0 and entry[26:28]==b'\0\0' and entry[13]==long_sum:
                        long_parts[sequence]=entry[1:11]+entry[14:26]+entry[28:32]
                    else:long_parts={};long_total=0;long_sum=None
                    continue
                short=entry[:11]
                base=short[:8].decode('ascii','replace').rstrip();extension=short[8:].decode('ascii','replace').rstrip()
                name=base+('.'+extension if extension else '')
                if long_total and set(long_parts)==set(range(1,long_total+1)) and short_checksum(short)==long_sum:
                    encoded=b''.join(long_parts[index] for index in range(1,long_total+1))
                    units=[encoded[index:index+2] for index in range(0,len(encoded),2)]
                    stop=next((index for index,unit in enumerate(units) if unit in (b'\0\0',b'\xff\xff')),len(units))
                    try:name=b''.join(units[:stop]).decode('utf-16le')
                    except UnicodeDecodeError:pass
                long_parts={};long_total=0;long_sum=None
                if entry[11]&8:continue
                first_cluster=(struct.unpack_from('<H',entry,20)[0]<<16)|struct.unpack_from('<H',entry,26)[0]
                yield {'name':name,'cluster':first_cluster,'bytes':struct.unpack_from('<I',entry,28)[0],
                       'directory':bool(entry[11]&16)}

    def lookup(self,path):
        parts=path.split('/')
        need(parts and all(part not in ('','.','..') and '\\' not in part for part in parts),'Invalid FAT path')
        directory=self.root
        for index,part in enumerate(parts):
            matches=[entry for entry in self._entries(directory) if entry['name'].casefold()==part.casefold()]
            need(len(matches)==1,'FAT path is missing or ambiguous: '+path)
            entry=matches[0]
            if index<len(parts)-1:
                need(entry['directory'],'FAT path component is not a directory')
                directory=entry['cluster']
        need(not entry['directory'],'Expected a FAT file')
        need(0<entry['bytes']<=128*BLOCK,'BOOT file has an invalid bounded directory size')
        return entry

    def copy_file(self,path,target,progress=None):
        entry=self.lookup(path);remaining=entry['bytes']
        needed=(remaining+self.cluster_bytes-1)//self.cluster_bytes
        chain=[]
        for cluster in self._chain(entry['cluster']):
            chain.append(cluster)
            if len(chain)==needed:break
        need(len(chain)==needed,'FAT file chain is shorter than its directory size')
        digest=hashlib.sha256();copied=0;index=0
        with target.open('xb') as stream:
            while index<len(chain):
                end=index+1
                while end<len(chain) and chain[end]==chain[end-1]+1 and (end-index+1)*self.cluster_bytes<=BLOCK:end+=1
                wanted=min(remaining,(end-index)*self.cluster_bytes)
                disk_bytes=(wanted+511)//512*512
                payload=self._read(self._cluster(chain[index]),disk_bytes)[:wanted]
                stream.write(payload);digest.update(payload)
                copied+=len(payload);remaining-=len(payload);index=end
                if progress is not None:progress(copied,entry['bytes'])
            need(remaining==0,'FAT file capture did not reach its directory size')
            stream.flush();os.fsync(stream.fileno())
        return {'bytes':copied,'directory_bytes':entry['bytes'],'sha256':digest.hexdigest(),
                'readback_method':'fat32-directory-and-sector-reads'}
